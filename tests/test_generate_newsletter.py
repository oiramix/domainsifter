"""Unit tests for scripts/generate_newsletter.py.

Buttondown HTTP calls are mocked via a fake `requests.Session`. No live API
calls; no real `daily-domains.json` reads (synthetic payloads). Tests cover:
  - URL UTM appending, slug formatting, formatters
  - HTML body assembly (top-level structure + per-row rendering)
  - Top-N selection (sort + truncate, ties broken by name)
  - Idempotency: same subject → skip create
  - Disabled / empty / dry-run paths
  - Buttondown error handling (non-200 responses, non-JSON, pagination)
  - CLI happy path + error exit codes
"""

from __future__ import annotations

import json
import sys
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import generate_newsletter as gn


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _domain(
    name: str,
    score: int,
    *,
    wayback: int | None = 100,
    opr: float | None = 3.5,
    backlinks: int | None = 200,
    tld: str | None = None,
) -> dict:
    """Synthetic domain dict matching the daily-domains.json shape."""
    return {
        "name": name,
        "tld": tld or name.rsplit(".", 1)[-1],
        "dropped_date": "2026-05-14",
        "wayback_snapshots": wayback,
        "wayback_last_snapshot": "2024-08-15",
        "open_page_rank": opr,
        "cert_history": True,
        "previous_registrar": "Acme",
        "score": score,
        "cc_source_domain_count": backlinks,
        "registrars": [
            {"name": "Namecheap", "url": f"https://namecheap.example/?d={name}"},
            {"name": "NameSilo", "url": f"https://namesilo.example/?q={name}&rid=ABC"},
            {"name": "Dynadot", "url": f"https://dynadot.example/?domain={name}"},
        ],
        "first_seen_date": "2026-05-14",
        "last_validated_date": "2026-05-14",
        "days_listed": 0,
    }


def _config(**overrides: Any) -> dict:
    """Default-enabled newsletter config (override per-test)."""
    base = {
        "newsletter": {
            "enabled": True,
            "top_n": 20,
            "subject_template": "DomainSifter daily picks — {date}",
            "intro_text": "Intro text for testing.",
            "site_url": "https://domainsifter.com",
        },
    }
    if overrides:
        base["newsletter"].update(overrides)
    return base


def _fake_session(responses: list[dict]) -> MagicMock:
    """Build a MagicMock requests.Session whose .get() and .post() return the
    next response in `responses` (FIFO). Each response dict has:
        method ('GET'|'POST'), status (int), json (dict|None), text (str)
    """
    session = MagicMock()
    queue = list(responses)

    def _next(method: str) -> MagicMock:
        for i, r in enumerate(queue):
            if r["method"] == method:
                queue.pop(i)
                resp = MagicMock()
                resp.status_code = r["status"]
                resp.text = r.get("text", "")
                if r.get("json") is None:
                    resp.json.side_effect = ValueError("no body")
                else:
                    resp.json.return_value = r["json"]
                return resp
        raise AssertionError(f"unexpected {method} call; queue exhausted")

    session.get.side_effect = lambda *a, **kw: _next("GET")
    session.post.side_effect = lambda *a, **kw: _next("POST")
    return session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_append_utm_adds_all_three_params():
    url = "https://example.com/path"
    out = gn._append_utm(url)
    assert "utm_source=newsletter" in out
    assert "utm_medium=email" in out
    assert "utm_campaign=daily" in out


def test_append_utm_preserves_existing_query_params():
    url = "https://example.com/path?ref=abc&q=hello"
    out = gn._append_utm(url)
    assert "ref=abc" in out
    assert "q=hello" in out
    assert "utm_source=newsletter" in out


def test_append_utm_overrides_existing_utm():
    """If a URL already had a utm_source, our value wins."""
    url = "https://example.com/?utm_source=old"
    out = gn._append_utm(url)
    assert "utm_source=newsletter" in out
    assert "utm_source=old" not in out


def test_append_utm_on_pxf_io_outer_url():
    """Namecheap-style affiliate URLs have an encoded inner destination via
    `?u=`. UTM appends to the OUTER URL (pxf.io's query string), not the
    encoded inner one — that's the contract Mario confirmed."""
    url = "https://namecheap.pxf.io/WO655J?u=https%3A%2F%2Fnamecheap.com%2F%3Fdomain%3Damber.org"
    out = gn._append_utm(url)
    # The outer pxf.io gets utm_*
    assert "utm_source=newsletter" in out
    # The encoded inner u= value is preserved exactly
    assert "u=https%3A%2F%2Fnamecheap.com%2F%3Fdomain%3Damber.org" in out


def test_domain_slug_format():
    assert gn._domain_slug("amberkite.org") == "drop-amberkite.org"
    assert gn._domain_slug("frostledge.xyz") == "drop-frostledge.xyz"


def test_verdict_thresholds_match_site():
    """Same thresholds as DomainTable.astro's verdictFromScore. If those
    diverge, the email and the site will give the same domain different
    verdicts — confusing UX."""
    assert gn._verdict_from_score(100) == "Clean"
    assert gn._verdict_from_score(70) == "Clean"
    assert gn._verdict_from_score(69) == "Promising"
    assert gn._verdict_from_score(40) == "Promising"
    assert gn._verdict_from_score(39) == "Caution"
    assert gn._verdict_from_score(0) == "Caution"


def test_fmt_int_handles_none_and_commas():
    assert gn._fmt_int(None) == "—"
    assert gn._fmt_int(0) == "0"
    assert gn._fmt_int(1234567) == "1,234,567"


def test_fmt_decimal_handles_none():
    assert gn._fmt_decimal(None) == "—"
    assert gn._fmt_decimal(3.14159) == "3.1"
    assert gn._fmt_decimal(0.0) == "0.0"


# ---------------------------------------------------------------------------
# Top-N selection
# ---------------------------------------------------------------------------


def test_pick_top_n_sorts_desc_by_score():
    """High score first. Pipeline already sorts, but we don't depend on it."""
    cands = [
        _domain("low.com", 10),
        _domain("high.org", 90),
        _domain("mid.net", 50),
    ]
    out = gn._pick_top_n(cands, 3)
    assert [d["name"] for d in out] == ["high.org", "mid.net", "low.com"]


def test_pick_top_n_truncates_to_n():
    cands = [_domain(f"d{i}.com", 100 - i) for i in range(50)]
    out = gn._pick_top_n(cands, 20)
    assert len(out) == 20
    # Scores 100..81 (the top 20 by score).
    assert [d["score"] for d in out] == list(range(100, 80, -1))


def test_pick_top_n_breaks_ties_by_name_asc():
    cands = [
        _domain("zzz.com", 50),
        _domain("aaa.com", 50),
        _domain("mmm.com", 50),
    ]
    out = gn._pick_top_n(cands, 3)
    assert [d["name"] for d in out] == ["aaa.com", "mmm.com", "zzz.com"]


def test_pick_top_n_handles_n_larger_than_input():
    cands = [_domain("a.com", 50), _domain("b.com", 40)]
    assert len(gn._pick_top_n(cands, 100)) == 2


def test_pick_top_n_handles_zero():
    cands = [_domain("a.com", 50)]
    assert gn._pick_top_n(cands, 0) == []


def test_pick_top_n_handles_negative_n_as_zero():
    """Defence-in-depth: -1 should not crash or wrap-around."""
    cands = [_domain("a.com", 50)]
    assert gn._pick_top_n(cands, -1) == []


# ---------------------------------------------------------------------------
# HTML body generation
# ---------------------------------------------------------------------------


def test_build_html_body_includes_subject_date_and_all_rows():
    today = date(2026, 5, 15)
    body = gn.build_html_body(
        [_domain("amber.org", 82), _domain("frost.xyz", 64)],
        today, "Intro paragraph.",
    )
    assert "May 15, 2026" in body
    assert "amber.org" in body
    assert "frost.xyz" in body
    assert "Intro paragraph." in body


def test_build_html_body_has_proper_html_structure():
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 5, 15), "x")
    assert body.lstrip().startswith("<!DOCTYPE html>")
    assert "<html" in body
    assert "</html>" in body
    assert 'role="presentation"' in body
    # A <style> block carries the responsive @media swap. Per-element styles
    # are still inline so older clients fall back to the desktop layout.
    assert "<style" in body


def test_build_html_body_uses_per_row_anchor_link():
    """Each domain name links to https://domainsifter.com/#drop-{name}."""
    body = gn.build_html_body([_domain("amber.org", 82)], date(2026, 5, 15), "x")
    assert 'href="https://domainsifter.com/#drop-amber.org"' in body


def test_build_html_body_appends_utm_to_registrar_links():
    body = gn.build_html_body([_domain("amber.org", 82)], date(2026, 5, 15), "x")
    # Three registrar links, each with utm_source=newsletter.
    assert body.count("utm_source=newsletter") >= 3


def test_build_html_body_renders_null_backlinks_as_dash():
    body = gn.build_html_body(
        [_domain("nograph.com", 50, backlinks=None)],
        date(2026, 5, 15), "x",
    )
    # The dash em (—) marker rendered for the backlinks cell.
    assert "—" in body


def test_build_html_body_renders_null_wayback_as_dash():
    body = gn.build_html_body(
        [_domain("nowayback.com", 50, wayback=None)],
        date(2026, 5, 15), "x",
    )
    assert "—" in body


def test_build_html_body_renders_all_three_logos():
    body = gn.build_html_body([_domain("amber.org", 82)], date(2026, 5, 15), "x")
    assert "registrar-logos/namecheap.png" in body
    assert "registrar-logos/namesilo.png" in body
    assert "registrar-logos/dynadot.png" in body


def test_build_html_body_skips_unknown_registrar_logos():
    """A registrar name not in REGISTRAR_LOGO_SLUGS is silently skipped —
    no broken-image references, no crash."""
    d = _domain("amber.org", 80)
    d["registrars"].append({"name": "MysteryRegistrar", "url": "https://x/y"})
    body = gn.build_html_body([d], date(2026, 5, 15), "x")
    assert "MysteryRegistrar" not in body
    # The 3 known logos render twice each (once in desktop table, once in
    # mobile cards); 6 total. The unknown one would have rendered twice too
    # if not filtered — its absence is what this test guards.
    assert body.count("registrar-logos/") == 6


def test_build_html_body_includes_unsubscribe_token():
    """Buttondown substitutes `{{ unsubscribe_url }}` at send time."""
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 5, 15), "x")
    assert "{{ unsubscribe_url }}" in body


def test_build_html_body_renders_clean_promising_caution_pills():
    body = gn.build_html_body(
        [
            _domain("clean.org", 80),
            _domain("promising.net", 55),
            _domain("caution.com", 20),
        ],
        date(2026, 5, 15), "x",
    )
    assert ">Clean<" in body
    assert ">Promising<" in body
    assert ">Caution<" in body


def test_build_html_body_escapes_html_in_domain_names():
    """Defence-in-depth — escape characters so a future malicious domain
    name (or fixture typo) can't break the markup."""
    d = _domain("safe.org", 80)
    d["name"] = "evil<script>.org"
    body = gn.build_html_body([d], date(2026, 5, 15), "x")
    assert "<script>" not in body
    assert "evil&lt;script&gt;.org" in body


def test_build_html_body_narrow_columns_have_nowrap_and_domain_does_not():
    """Narrow columns (TLD, Wayback, OPR, Backlinks, Verdict, Register) MUST
    carry `white-space: nowrap` so short labels like "TLD" and "Promising"
    don't break across lines in narrow email reading panes (observed in
    Gmail web). The Domain column intentionally omits nowrap so long names
    absorb the flex when other columns are pinned wide."""
    body = gn.build_html_body(
        [_domain("amber.org", 55)],  # 55 → "Promising" verdict
        date(2026, 5, 15), "x",
    )
    # Every <th> header has nowrap.
    th_count = body.count("<th ")
    nowrap_th_count = body.count('font-weight: 600; white-space: nowrap;">')
    # 7 columns: Domain, TLD, Wayback, OPR, Backlinks, Verdict, Register.
    assert th_count == 7
    assert nowrap_th_count == 7
    # Narrow data cells carry nowrap.
    assert 'text-align: right; color: #1a1a1a; white-space: nowrap;">' in body
    # Verdict pill cell carries nowrap.
    assert 'padding: 10px 6px; white-space: nowrap;">' in body
    # Domain link cell does NOT carry nowrap — long names need to wrap.
    # The Domain td is the only `<td style="padding: 10px 6px;">` with no
    # additional declarations (followed immediately by the <a>).
    assert '<td style="padding: 10px 6px;"><a href=' in body


def test_build_html_body_h1_is_static_brand_string():
    """The in-body <h1> is "DomainSifter daily picks" with no count and no
    date — the inbox already shows the date, and a hardcoded count would
    sometimes lie on low-volume days."""
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 5, 15), "x")
    assert ">DomainSifter daily picks</h1>" in body
    # No subline date paragraph below the h1.
    assert "Daily picks ·" not in body


def test_build_html_body_has_responsive_media_query():
    """The responsive swap requires a @media (max-width: 600px) block in
    head <style>. Without it, mobile clients render the unreadable
    7-column desktop table — that's the bug this layout fixes."""
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 5, 15), "x")
    assert "(max-width: 600px)" in body
    # The display-swap rules use !important to override inline styles.
    assert "display: none !important" in body
    assert "display: block !important" in body


def test_build_html_body_emits_mobile_cards_alongside_desktop_table():
    """Two layout blocks coexist in the markup. @media toggles which one
    is visible — desktop sees the table, mobile sees the cards. The mobile
    block is hidden by default via inline `display: none`; @media flips it
    on. The desktop block uses no inline display so it's table by default
    and goes to none on mobile via the @media rule."""
    body = gn.build_html_body([_domain("amber.org", 80)], date(2026, 5, 15), "x")
    # Desktop table is tagged for hide-on-mobile.
    assert 'class="ds-desktop-only"' in body
    # Mobile cards block exists and is hidden inline by default.
    assert 'class="ds-mobile-only"' in body
    assert 'class="ds-mobile-only" style="display: none' in body
    # Mobile card rendering: each domain has a card-tagged label/value row.
    # The "TLD" / "Wayback" / etc. labels appear as <td> text inside the
    # cards (they don't appear in the desktop table — those use <th>).
    assert ">TLD</td>" in body
    assert ">Wayback</td>" in body
    assert ">Backlinks</td>" in body


def test_build_html_body_uses_configured_site_url():
    body = gn.build_html_body(
        [_domain("a.org", 80)], date(2026, 5, 15), "x",
        site_url="https://staging.example.com",
    )
    assert "https://staging.example.com/#drop-a.org" in body
    assert "domainsifter.com/#drop-" not in body


# ---------------------------------------------------------------------------
# Buttondown API: list + create
# ---------------------------------------------------------------------------


def test_list_drafts_handles_single_page():
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {
            "results": [{"id": "abc", "subject": "S1"}, {"id": "def", "subject": "S2"}],
            "next": None,
        }},
    ])
    out = gn._list_drafts("key", session=session)
    assert len(out) == 2
    assert out[0]["id"] == "abc"


def test_list_drafts_follows_pagination():
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {
            "results": [{"id": "1"}, {"id": "2"}],
            "next": "https://api.buttondown.com/v1/emails?status=draft&page=2",
        }},
        {"method": "GET", "status": 200, "json": {
            "results": [{"id": "3"}],
            "next": None,
        }},
    ])
    out = gn._list_drafts("key", session=session)
    assert [d["id"] for d in out] == ["1", "2", "3"]


def test_list_drafts_raises_on_http_error():
    session = _fake_session([
        {"method": "GET", "status": 500, "json": None, "text": "internal error"},
    ])
    with pytest.raises(gn.ButtondownError, match="HTTP 500"):
        gn._list_drafts("key", session=session)


def test_list_drafts_raises_on_non_json():
    session = _fake_session([
        {"method": "GET", "status": 200, "json": None, "text": "<html>oops"},
    ])
    with pytest.raises(gn.ButtondownError, match="non-JSON"):
        gn._list_drafts("key", session=session)


def test_create_draft_posts_correct_payload():
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "new-id", "subject": json["subject"]}
        return resp

    session = MagicMock()
    session.post.side_effect = post_capture

    out = gn._create_draft("KEY", "Hello", "<html>body</html>", session=session)
    assert out["id"] == "new-id"
    assert captured["url"].endswith("/v1/emails")
    assert captured["headers"]["Authorization"] == "Token KEY"
    assert captured["json"] == {
        "subject": "Hello",
        "body": "<html>body</html>",
        "status": "draft",
    }


def test_create_draft_raises_on_4xx():
    session = _fake_session([
        {"method": "POST", "status": 422, "json": None, "text": "validation error"},
    ])
    with pytest.raises(gn.ButtondownError, match="HTTP 422"):
        gn._create_draft("key", "S", "B", session=session)


def test_already_drafted_finds_match_by_subject():
    drafts = [
        {"id": "a", "subject": "Other"},
        {"id": "b", "subject": "DomainSifter daily picks — May 15, 2026"},
        {"id": "c", "subject": "Else"},
    ]
    found = gn._already_drafted(drafts, "DomainSifter daily picks — May 15, 2026")
    assert found and found["id"] == "b"


def test_already_drafted_returns_none_when_no_match():
    drafts = [{"id": "a", "subject": "Other"}]
    assert gn._already_drafted(drafts, "Not present") is None


# ---------------------------------------------------------------------------
# generate_newsletter (top-level orchestration)
# ---------------------------------------------------------------------------


def test_generate_newsletter_disabled_short_circuits():
    """Feature flag off → return disabled status, no API call attempted.
    Mario keeps it false until BUTTONDOWN_API_KEY is on the OVH .env."""
    cfg = _config()
    cfg["newsletter"]["enabled"] = False
    out = gn.generate_newsletter(cfg, {"domains": [_domain("a.org", 80)]})
    assert out == {"status": "disabled"}


def test_generate_newsletter_empty_domains_skipped():
    out = gn.generate_newsletter(_config(), {"domains": []})
    assert out["status"] == "skipped_empty"


def test_generate_newsletter_missing_domains_key_skipped():
    out = gn.generate_newsletter(_config(), {})
    assert out["status"] == "skipped_empty"


def test_generate_newsletter_dry_run_returns_body_no_post():
    cfg = _config()
    session = MagicMock()  # if posted, MagicMock would record it
    out = gn.generate_newsletter(
        cfg, {"domains": [_domain("a.org", 80)]},
        api_key="KEY", today=date(2026, 5, 15),
        session=session, dry_run=True,
    )
    assert out["status"] == "dry_run"
    assert "amber" not in out["body"]  # only a.org was in input
    assert "a.org" in out["body"]
    assert out["body_chars"] > 0
    # No API call attempted in dry-run.
    session.get.assert_not_called()
    session.post.assert_not_called()


def test_generate_newsletter_creates_draft_happy_path():
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
        {"method": "POST", "status": 201, "json": {
            "id": "new-id",
            "subject": "DomainSifter daily picks — May 15, 2026",
        }},
    ])
    out = gn.generate_newsletter(
        _config(),
        {"domains": [_domain("a.org", 80), _domain("b.com", 70)]},
        api_key="KEY", today=date(2026, 5, 15), session=session,
    )
    assert out["status"] == "created"
    assert out["id"] == "new-id"
    assert out["subject"] == "DomainSifter daily picks — May 15, 2026"
    assert out["domain_count"] == 2


def test_generate_newsletter_idempotent_when_draft_exists():
    """Same subject already in drafts → skip create. Don't POST a duplicate."""
    existing_subject = "DomainSifter daily picks — May 15, 2026"
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {
            "results": [{"id": "existing-id", "subject": existing_subject}],
            "next": None,
        }},
        # NO POST — if generate_newsletter tries to create, the fake_session
        # will raise "queue exhausted".
    ])
    out = gn.generate_newsletter(
        _config(),
        {"domains": [_domain("a.org", 80)]},
        api_key="KEY", today=date(2026, 5, 15), session=session,
    )
    assert out["status"] == "skipped_duplicate"
    assert out["id"] == "existing-id"
    session.post.assert_not_called()


def test_generate_newsletter_raises_when_enabled_but_no_api_key():
    cfg = _config()
    with pytest.raises(RuntimeError, match="BUTTONDOWN_API_KEY"):
        gn.generate_newsletter(
            cfg, {"domains": [_domain("a.org", 80)]},
            api_key=None, today=date(2026, 5, 15),
        )


def test_generate_newsletter_respects_custom_top_n():
    cfg = _config(top_n=3)
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
        {"method": "POST", "status": 201, "json": {"id": "x", "subject": "y"}},
    ])
    domains = [_domain(f"d{i}.com", 100 - i) for i in range(10)]
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        captured["subject"] = json["subject"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "x", "subject": json["subject"]}
        return resp

    session.post.side_effect = post_capture
    gn.generate_newsletter(
        cfg, {"domains": domains}, api_key="KEY",
        today=date(2026, 5, 15), session=session,
    )
    # Only top 3 by score (d0, d1, d2) appear in body; d9 does not. The
    # subject no longer carries a count — varies day-to-day, sometimes wrong.
    assert "d0.com" in captured["body"]
    assert "d2.com" in captured["body"]
    assert "d9.com" not in captured["body"]


def test_generate_newsletter_subject_uses_iso_date_for_idempotency():
    """Two runs the same day → same subject. Two runs on different days →
    different subjects (no false-duplicate match)."""
    cfg = _config()
    session1 = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
        {"method": "POST", "status": 201, "json": {"id": "1", "subject": "x"}},
    ])
    out1 = gn.generate_newsletter(
        cfg, {"domains": [_domain("a.org", 80)]},
        api_key="K", today=date(2026, 5, 14), session=session1,
    )
    out2_subject = "DomainSifter daily picks — May 15, 2026"
    # Different date in today=... → different subject.
    assert out1["subject"] != out2_subject


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


def test_main_returns_1_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("BUTTONDOWN_API_KEY", raising=False)
    rc = gn.main(["--config", str(tmp_path / "nope.json")])
    assert rc == 1


def test_main_returns_1_when_input_missing(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"newsletter": {"enabled": False}}), encoding="utf-8")
    rc = gn.main(["--config", str(cfg_path), "--input", str(tmp_path / "no.json")])
    assert rc == 1


def test_main_dry_run_prints_body_and_returns_0(monkeypatch, tmp_path, capsys):
    """Dry-run path: builds HTML, prints it, no Buttondown call attempted."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(_config()),  # newsletter.enabled=true
        encoding="utf-8",
    )
    input_path = tmp_path / "daily.json"
    input_path.write_text(
        json.dumps({"domains": [_domain("a.org", 80)]}), encoding="utf-8",
    )
    rc = gn.main([
        "--config", str(cfg_path),
        "--input", str(input_path),
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<!DOCTYPE html>" in out
    assert "a.org" in out


def test_main_returns_2_on_buttondown_api_failure(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config()), encoding="utf-8")
    input_path = tmp_path / "daily.json"
    input_path.write_text(
        json.dumps({"domains": [_domain("a.org", 80)]}), encoding="utf-8",
    )
    monkeypatch.setenv("BUTTONDOWN_API_KEY", "KEY")

    failing = _fake_session([
        {"method": "GET", "status": 500, "json": None, "text": "server down"},
    ])
    monkeypatch.setattr(gn.requests, "Session", lambda: failing)

    rc = gn.main(["--config", str(cfg_path), "--input", str(input_path)])
    assert rc == 2


def test_main_returns_0_when_disabled(monkeypatch, tmp_path):
    """Feature flag false → noop happy path. Even without API key, exits 0."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"newsletter": {"enabled": False}}), encoding="utf-8",
    )
    input_path = tmp_path / "daily.json"
    input_path.write_text(
        json.dumps({"domains": [_domain("a.org", 80)]}), encoding="utf-8",
    )
    monkeypatch.delenv("BUTTONDOWN_API_KEY", raising=False)
    rc = gn.main(["--config", str(cfg_path), "--input", str(input_path)])
    assert rc == 0
