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
from unittest.mock import MagicMock, patch

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
    """Default-enabled newsletter config (override per-test).

    The two sidecar paths point at files that do not exist so unit tests
    never read the repo's real src/data/*.json. Tests that want evidence
    wired in write their own fixtures to tmp_path and override these keys.
    """
    base = {
        "newsletter": {
            "enabled": True,
            "top_n": 20,
            "subject_template": "DomainSifter daily picks — {date}",
            "intro_text": "Intro text for testing.",
            "site_url": "https://domainsifter.com",
            "sidecar_excerpts_path": "tests/__no_such_sidecar__.json",
            "archive_index_path": "tests/__no_such_archive_index__.json",
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


def test_verdict_from_score_fallback_thresholds():
    """The score-only fallback path used when payloads predate the
    2026-05-17 server-computed verdict field (sample-domains.json,
    legacy JSON). Production payloads carry an explicit verdict and
    _verdict_for_domain reads that instead — see the next test."""
    assert gn._verdict_from_score(100) == "Clean"
    assert gn._verdict_from_score(70) == "Clean"
    assert gn._verdict_from_score(69) == "Promising"
    assert gn._verdict_from_score(40) == "Promising"
    assert gn._verdict_from_score(39) == "Caution"
    assert gn._verdict_from_score(0) == "Caution"


def test_verdict_for_domain_prefers_server_field():
    """When the JSON entry carries an explicit `verdict`, that wins over
    the score-only fallback — tightened Promising rules live in
    scripts/output.py and the newsletter just renders the decision."""
    # Server says Caution despite a high score — newsletter must honor it
    # (e.g., a soft-signal dating domain that scored 90).
    assert gn._verdict_for_domain({"score": 90, "verdict": "Caution"}) == "Caution"
    # Server says Promising; newsletter uses that label even if the
    # score-only fallback would have agreed.
    assert gn._verdict_for_domain({"score": 55, "verdict": "Promising"}) == "Promising"
    # Server says Clean.
    assert gn._verdict_for_domain({"score": 75, "verdict": "Clean"}) == "Clean"


def test_verdict_for_domain_falls_back_to_score_when_missing():
    """No verdict field (older JSON, sample data) → fall back to
    score-only thresholds."""
    assert gn._verdict_for_domain({"score": 80}) == "Clean"
    assert gn._verdict_for_domain({"score": 55}) == "Promising"
    assert gn._verdict_for_domain({"score": 20}) == "Caution"


def test_verdict_for_domain_falls_back_when_value_invalid():
    """A malformed verdict (non-string, unknown enum) → fall back to score."""
    assert gn._verdict_for_domain({"score": 80, "verdict": "Unknown"}) == "Clean"
    assert gn._verdict_for_domain({"score": 80, "verdict": None}) == "Clean"
    assert gn._verdict_for_domain({"score": 20, "verdict": 123}) == "Caution"


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
# Per-TLD diversity cap (added 2026-05-25)
# ---------------------------------------------------------------------------


def test_apply_per_tld_cap_clips_over_represented_tld():
    """One TLD with 17 entries + one with 1 entry + cap=8 → kept set has
    8 from the big TLD and 1 from the small TLD. Matches Mario's expected
    eyeball-test transformation of today=17.net+1.org → 8.net+1.org."""
    cands = (
        [_domain(f"n{i:02d}.net", 100 - i, tld="net") for i in range(17)]
        + [_domain("solo.org", 50, tld="org")]
    )
    out = gn._apply_per_tld_cap(cands, max_per_tld=8)
    by_tld = {}
    for d in out:
        by_tld.setdefault(d["tld"], []).append(d)
    assert len(by_tld["net"]) == 8
    assert len(by_tld["org"]) == 1
    # Top 8 .net by score: scores 100..93. Last 9 (.net scores 92..84) dropped.
    kept_net_scores = sorted((d["score"] for d in by_tld["net"]), reverse=True)
    assert kept_net_scores == [100, 99, 98, 97, 96, 95, 94, 93]


def test_apply_per_tld_cap_noop_when_no_tld_exceeds_cap():
    """All TLDs already at or below the cap → input order preserved by
    TLD bucket (caller score-sorts afterwards). Length equals input length."""
    cands = [
        _domain("a.net", 90, tld="net"),
        _domain("b.net", 80, tld="net"),
        _domain("c.org", 70, tld="org"),
        _domain("d.xyz", 60, tld="xyz"),
    ]
    out = gn._apply_per_tld_cap(cands, max_per_tld=8)
    assert len(out) == 4
    assert {d["name"] for d in out} == {"a.net", "b.net", "c.org", "d.xyz"}


def test_apply_per_tld_cap_slack_flows_when_small_tld_short_of_cap():
    """A TLD with fewer than `max_per_tld` candidates contributes everything
    it has. The function itself doesn't decide the panel slot allocation —
    that's the downstream score-sort + slice — but the slack-flow behaviour
    relies on this returning the full short-TLD bucket so the caller has
    raw material to choose from."""
    cands = (
        [_domain(f"n{i}.net", 100 - i, tld="net") for i in range(20)]  # 20 .net
        + [_domain("o1.org", 95, tld="org"), _domain("o2.org", 85, tld="org")]  # 2 .org
        + [_domain("x1.xyz", 92, tld="xyz")]                                      # 1 .xyz
    )
    out = gn._apply_per_tld_cap(cands, max_per_tld=8)
    by_tld = {}
    for d in out:
        by_tld.setdefault(d["tld"], []).append(d)
    assert len(by_tld["net"]) == 8
    assert len(by_tld["org"]) == 2     # full short bucket preserved
    assert len(by_tld["xyz"]) == 1     # full short bucket preserved


def test_apply_per_tld_cap_keeps_highest_scoring_per_tld():
    """Within each TLD bucket, highest score wins the cap slots. Quality-first
    within the cap is the binding rule from the spec."""
    cands = [
        _domain("low.net",  10, tld="net"),
        _domain("mid.net",  50, tld="net"),
        _domain("hi.net",   90, tld="net"),
        _domain("tied1.net", 70, tld="net"),
        _domain("tied2.net", 70, tld="net"),
    ]
    out = gn._apply_per_tld_cap(cands, max_per_tld=2)
    kept_names = {d["name"] for d in out}
    # Top 2 by (score desc, name asc): hi.net (90) then tied1.net (70, name asc).
    assert kept_names == {"hi.net", "tied1.net"}


def test_apply_per_tld_cap_ties_broken_by_name_ascending():
    """Documented tie-break — keeps the algorithm reproducible across runs
    even when scores tie. Mirrors _pick_top_n's tie-break behaviour."""
    cands = [
        _domain("zulu.org",  70, tld="org"),
        _domain("alpha.org", 70, tld="org"),
        _domain("mike.org",  70, tld="org"),
    ]
    out = gn._apply_per_tld_cap(cands, max_per_tld=2)
    names = [d["name"] for d in out]
    # alpha < mike < zulu alphabetically → alpha and mike kept.
    assert set(names) == {"alpha.org", "mike.org"}


def test_apply_per_tld_cap_zero_disables_cap():
    """max_per_tld=0 → no-op. Same input set returned (order may be
    reordered by TLD bucket but contents are identical)."""
    cands = [_domain(f"n{i}.net", 100 - i, tld="net") for i in range(20)]
    out = gn._apply_per_tld_cap(cands, max_per_tld=0)
    assert len(out) == 20
    assert {d["name"] for d in out} == {d["name"] for d in cands}


def test_apply_per_tld_cap_negative_disables_cap():
    """Defensive: negative max_per_tld behaves like 0 (disabled). Prevents
    a typo or test-fixture mistake from silently dropping every entry."""
    cands = [_domain(f"n{i}.net", 100 - i, tld="net") for i in range(5)]
    out = gn._apply_per_tld_cap(cands, max_per_tld=-1)
    assert len(out) == 5


def test_apply_per_tld_cap_empty_input():
    """Empty list → empty list. Trivial but documents the contract."""
    assert gn._apply_per_tld_cap([], max_per_tld=8) == []


# ---------------------------------------------------------------------------
# Fresh-today filter (added 2026-05-17)
# ---------------------------------------------------------------------------


def test_filter_to_fresh_today_keeps_only_days_listed_zero():
    cands = [
        _domain("fresh1.com", 80, tld="com"),     # days_listed=0 by default
        {**_domain("old1.com", 90), "days_listed": 1},
        {**_domain("old14.com", 95), "days_listed": 14},
        _domain("fresh2.org", 70),
    ]
    out = gn._filter_to_fresh_today(cands)
    assert {d["name"] for d in out} == {"fresh1.com", "fresh2.org"}


def test_filter_to_fresh_today_treats_missing_days_listed_as_zero():
    """Sample-domains.json may omit the field; the frontend treats missing
    as today, so the newsletter does too. Keeps the preview-data path
    rendering something instead of an empty draft on cold-start."""
    cands = [{"name": "legacy.com", "score": 80, "registrars": []}]
    assert len(gn._filter_to_fresh_today(cands)) == 1


def test_filter_to_fresh_today_returns_empty_when_all_carryover():
    cands = [
        {**_domain("a.com", 80), "days_listed": 1},
        {**_domain("b.com", 90), "days_listed": 7},
    ]
    assert gn._filter_to_fresh_today(cands) == []


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
    # 3 known logos render once each (single-table layout, no duplication).
    assert body.count("registrar-logos/") == 3


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


def test_build_html_body_register_cell_carries_ds_register_class():
    """Both the Register <th> (header) and <td> (data cell) carry the
    `ds-register-cell` class so the @media (max-width: 600px) rule can
    hide the header and promote the data cell to a full-width block —
    putting the registrar logos on their own line below the data cells."""
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 5, 15), "x")
    # One <th> with the class.
    assert 'th class="ds-register-cell"' in body
    # One <td> per data row.
    assert 'td class="ds-register-cell"' in body
    # 2 markup attributes (1 th + 1 td); the class name also appears in the
    # @media CSS selectors but that's counted separately by inclusion above.
    assert body.count('class="ds-register-cell"') == 2


def test_build_html_body_does_not_mention_estonia():
    """The 'Estonia-based independent project' sentence was removed earlier.
    Regression guard against it being reintroduced — the phrasing did no
    positioning work and just added byte count."""
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 5, 15), "x")
    assert "Estonia" not in body
    assert "Estonian" not in body


def test_build_html_body_under_gmail_clip_limit_at_20_domains():
    """Gmail clips messages over ~102 KB ('[Message clipped] View entire
    message'). Commit 8e479a7 emitted each domain twice (desktop table +
    mobile cards) and crossed that line. Single-table layout must stay
    well under it — assert under 90,000 bytes at 20 domains, which is
    the realistic top-of-cluster daily count."""
    domains = [_domain(f"sample{i}.org", 80 - i) for i in range(20)]
    body = gn.build_html_body(
        domains, date(2026, 5, 15),
        "Intro paragraph used for the byte-budget regression test.",
    )
    size = len(body.encode("utf-8"))
    assert size < 90_000, (
        f"Body is {size} bytes; Gmail clips above ~102 KB and the 90 KB "
        f"ceiling is the safety margin under that limit."
    )


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


def test_generate_newsletter_skips_when_all_carryover():
    """Distinct status: JSON has entries but none are fresh-today. Was a
    silent quality loss before — the carryover would ship under the
    "today's drops" banner. Now returns a dedicated status and no API
    call fires (verified by passing no session — would AttributeError if
    POST was attempted)."""
    cands = [
        {**_domain("a.com", 80), "days_listed": 1},
        {**_domain("b.com", 90), "days_listed": 14},
        {**_domain("c.com", 75), "days_listed": 3},
    ]
    out = gn.generate_newsletter(
        _config(), {"domains": cands},
        api_key="KEY", today=date(2026, 5, 17),
    )
    assert out["status"] == "skipped_no_fresh"


def test_generate_newsletter_renders_only_fresh_in_mixed_payload():
    """7 fresh-today + 50 carryover → draft body contains the 7 fresh names
    and zero of the carryover names. Top-N cap (20) doesn't bite because
    fresh count (7) is below it."""
    fresh = [_domain(f"fresh{i}.com", 80 - i) for i in range(7)]
    carry = [
        {**_domain(f"old{i}.com", 95 - i), "days_listed": (i % 14) + 1}
        for i in range(50)
    ]
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    out = gn.generate_newsletter(
        _config(), {"domains": fresh + carry},
        api_key="KEY", today=date(2026, 5, 17), session=session,
    )
    assert out["status"] == "created"
    # Every fresh name appears; no carryover name does.
    for i in range(7):
        assert f"fresh{i}.com" in captured["body"]
    for i in range(50):
        assert f"old{i}.com" not in captured["body"], (
            f"carryover name 'old{i}.com' leaked into draft body"
        )


def test_generate_newsletter_top20_cap_within_fresh_set():
    """When fresh-today exceeds top_n, the cap applies to the fresh subset.
    25 fresh entries → top 20 by score appear; lowest-5 by score do not."""
    fresh = [_domain(f"f{i:02d}.com", 100 - i) for i in range(25)]
    carry = [{**_domain(f"c{i}.com", 99), "days_listed": 2} for i in range(10)]
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    out = gn.generate_newsletter(
        _config(), {"domains": carry + fresh},  # carryover ordered first
        api_key="KEY", today=date(2026, 5, 17), session=session,
    )
    assert out["status"] == "created"
    assert out["domain_count"] == 20
    # Top 20 fresh (scores 100..81) appear; bottom 5 fresh (scores 80..76) don't.
    for i in range(20):
        assert f"f{i:02d}.com" in captured["body"]
    for i in range(20, 25):
        assert f"f{i:02d}.com" not in captured["body"]
    # No carryover ever appears even though it scored 99.
    for i in range(10):
        assert f"c{i}.com" not in captured["body"]


def test_generate_newsletter_applies_per_tld_cap_when_configured():
    """End-to-end: when config.display_caps.max_per_tld_in_top_panel=8 is set
    AND fresh today is dominated by one TLD, the draft body contains at most
    8 entries from any single TLD. Mirrors the production scenario as of
    2026-05-25: ~17 .net + a sprinkling of other TLDs."""
    fresh_net = [_domain(f"n{i:02d}.net", 100 - i, tld="net") for i in range(17)]
    fresh_org = [_domain("solo.org", 75, tld="org")]
    fresh_xyz = [_domain("x1.xyz", 65, tld="xyz"), _domain("x2.xyz", 60, tld="xyz")]
    fresh = fresh_net + fresh_org + fresh_xyz
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        captured["subject"] = json["subject"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    cfg = _config()
    cfg["display_caps"] = {"max_per_tld_in_top_panel": 8}

    out = gn.generate_newsletter(
        cfg, {"domains": fresh},
        api_key="KEY", today=date(2026, 5, 25), session=session,
    )
    assert out["status"] == "created"
    # Top 8 .net by score (scores 100..93) appear; the remaining 9 .net don't.
    for i in range(8):
        assert f"n{i:02d}.net" in captured["body"]
    for i in range(8, 17):
        assert f"n{i:02d}.net" not in captured["body"], (
            f"per-TLD cap leaked: 'n{i:02d}.net' (score {100 - i}) shipped "
            f"despite 8 higher-scoring .net entries above it"
        )
    # Smaller-TLD entries below the cap survive untouched and fill slack slots.
    assert "solo.org" in captured["body"]
    assert "x1.xyz" in captured["body"]
    assert "x2.xyz" in captured["body"]


def test_generate_newsletter_per_tld_cap_disabled_by_default():
    """Backward-compat: a config without display_caps behaves exactly as
    before — score-sort across the fresh set, take top_n. The 17 .net
    entries here would crowd out everything else under the old behaviour,
    which is the very gap this feature exists to close; this test pins the
    pre-feature behaviour for any caller that omits the block."""
    fresh = [_domain(f"n{i:02d}.net", 100 - i, tld="net") for i in range(17)]
    fresh += [_domain("solo.org", 50, tld="org")]  # lowest score, fills 18th slot
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    # No display_caps key — old behaviour preserved.
    out = gn.generate_newsletter(
        _config(), {"domains": fresh},
        api_key="KEY", today=date(2026, 5, 25), session=session,
    )
    assert out["status"] == "created"
    # All 17 .net appear (top_n=20 is the only ceiling), plus solo.org.
    for i in range(17):
        assert f"n{i:02d}.net" in captured["body"]
    assert "solo.org" in captured["body"]


def test_generate_newsletter_per_tld_cap_no_effect_below_panel_size():
    """When fresh-today is small enough that no TLD has more than cap
    entries, the per-TLD cap doesn't bite. Body matches what you'd get
    without the cap. Same backward-compat guarantee as the disabled case,
    but at runtime instead of via config absence."""
    fresh = [
        _domain("a.net", 90, tld="net"),
        _domain("b.net", 80, tld="net"),
        _domain("c.org", 70, tld="org"),
        _domain("d.xyz", 60, tld="xyz"),
    ]
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    cfg = _config()
    cfg["display_caps"] = {"max_per_tld_in_top_panel": 8}

    out = gn.generate_newsletter(
        cfg, {"domains": fresh},
        api_key="KEY", today=date(2026, 5, 25), session=session,
    )
    assert out["status"] == "created"
    for name in ("a.net", "b.net", "c.org", "d.xyz"):
        assert name in captured["body"]


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


# ---------------------------------------------------------------------------
# Evidence rendering (2026-09-19): phase2_reason, archived titles, deep
# links, credibility line, featured/compact split, plain-text part.
# ---------------------------------------------------------------------------


# Hostile characters, spelled with chr() so this source file stays plain
# ASCII — a literal bidi override in a test file is exactly the trap these
# tests exist to guard against.
BIDI_OVERRIDE = chr(0x202E)     # RIGHT-TO-LEFT OVERRIDE
ISOLATE_START = chr(0x2066)     # LEFT-TO-RIGHT ISOLATE
ISOLATE_END = chr(0x2069)       # POP DIRECTIONAL ISOLATE
BELL = chr(0x07)                # C0 control
NUL = chr(0x00)
SOH = chr(0x01)
ELLIPSIS = chr(0x2026)
# "Tide tables and surf forecast", zh-CN — non-Latin archived titles are
# common in the sidecar and must survive cleaning intact.
CJK_TITLE = "".join(chr(c) for c in (0x6F6E, 0x6C50, 0x8868, 0x4E0E, 0x51B2,
                                     0x6D6A, 0x9884, 0x62A5))


def _excerpt(title: str | None, **extra: Any) -> dict:
    """One wayback_excerpts.json sidecar entry."""
    return {
        "snapshot_timestamp": "20260309025947",
        "snapshot_url": "http://web.archive.org/web/20260309025947/http://x/",
        "title": title,
        "meta_description": None,
        "h1": [],
        "h2": [],
        **extra,
    }


# --- Untrusted text cleaning -------------------------------------------------


def test_clean_untrusted_text_collapses_whitespace_and_newlines():
    assert gn._clean_untrusted_text("  Tide   tables\n\nand surf  ", 100) == (
        "Tide tables and surf"
    )


def test_clean_untrusted_text_strips_control_and_bidi_characters():
    """Bidi overrides silently reorder the text printed AROUND them - in an
    email that means an archived title could visually rewrite the domain
    name beside it. Controls and the bidi family become spaces."""
    hostile = f"safe{BIDI_OVERRIDE}txt.exe{BELL} title{ISOLATE_START}x{ISOLATE_END}"
    out = gn._clean_untrusted_text(hostile, 200)
    assert BIDI_OVERRIDE not in out
    assert ISOLATE_START not in out
    assert ISOLATE_END not in out
    assert BELL not in out
    assert "safe" in out and "title" in out


def test_clean_untrusted_text_caps_length_including_ellipsis():
    out = gn._clean_untrusted_text("x" * 5000, 120)
    assert len(out) == 120
    assert out.endswith(ELLIPSIS)


def test_clean_untrusted_text_preserves_non_latin_scripts():
    """Chinese/Thai/German titles are common in the sidecar and are real
    content - cleaning must not mangle them."""
    assert gn._clean_untrusted_text(CJK_TITLE, 100) == CJK_TITLE
    assert gn._clean_untrusted_text("Größe Straße", 100) == (
        "Größe Straße"
    )


def test_clean_untrusted_text_returns_none_for_junk():
    assert gn._clean_untrusted_text(None, 100) is None
    assert gn._clean_untrusted_text(123, 100) is None
    assert gn._clean_untrusted_text("   ", 100) is None
    assert gn._clean_untrusted_text(NUL + SOH, 100) is None


# --- phase2_reason -----------------------------------------------------------


def test_reason_text_returns_cleaned_reason():
    d = _domain("a.org", 80)
    d["phase2_reason"] = "clear compound word, brandable"
    assert gn._reason_text(d) == "clear compound word, brandable"


def test_reason_text_none_on_missing_null_and_junk():
    """Null on mechanical-fallback days and pre-2026-09-19 carryover; the
    render side does one None check for all of these."""
    assert gn._reason_text(_domain("a.org", 80)) is None            # absent
    assert gn._reason_text({"phase2_reason": None}) is None          # explicit null
    assert gn._reason_text({"phase2_reason": "   "}) is None         # blank
    assert gn._reason_text({"phase2_reason": 42}) is None            # junk type


def test_reason_text_drops_ranker_placeholder():
    """'missing from response' is a pipeline marker, not a justification."""
    assert gn._reason_text({"phase2_reason": "missing from response"}) is None
    assert gn._reason_text({"phase2_reason": "Missing From Response"}) is None


def test_build_html_body_renders_reason_under_featured_pick():
    d = _domain("marketglow.com", 80)
    d["phase2_reason"] = "clear compound word, brandable and memorable"
    body = gn.build_html_body([d], date(2026, 9, 19), "x", featured_n=1)
    assert "clear compound word, brandable and memorable" in body


def test_build_html_body_renders_reason_in_compact_row():
    d = _domain("tideblock.io", 60)
    d["phase2_reason"] = "short two-syllable tech name"
    body = gn.build_html_body([d], date(2026, 9, 19), "x")  # featured_n=0
    assert "short two-syllable tech name" in body


def test_build_html_body_omits_reason_element_entirely_when_null():
    """Missing reason must leave NO element behind - not an empty padded
    div, not a dash. The row is simply one line shorter."""
    with_reason = _domain("withreason.org", 80)
    with_reason["phase2_reason"] = "brandable compound"
    without = _domain("noreason.org", 80)
    body_with = gn.build_html_body([with_reason], date(2026, 9, 19), "x")
    body_without = gn.build_html_body([without], date(2026, 9, 19), "x")
    # The secondary-line div only exists in the with-reason render.
    assert "margin-top: 3px; font-size: 11px" in body_with
    assert "margin-top: 3px; font-size: 11px" not in body_without


def test_build_html_body_escapes_hostile_reason():
    """phase2_reason is model output - treat it as untrusted too."""
    d = _domain("a.org", 80)
    d["phase2_reason"] = "<script>alert(1)</script>"
    body = gn.build_html_body([d], date(2026, 9, 19), "x", featured_n=1)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# --- Wayback excerpt sidecar -------------------------------------------------


def test_excerpt_title_returns_cleaned_title():
    excerpts = {"marketglow.com": _excerpt("Celebrity net worth tracker")}
    assert gn._excerpt_title(excerpts, "marketglow.com", 120) == (
        "Celebrity net worth tracker"
    )


def test_excerpt_title_none_for_absent_null_entry_and_null_title():
    """Three sidecar states, all rendering nothing: key absent, key present
    but null (we looked, Wayback had nothing), title null/blank."""
    excerpts = {"nulled.org": None, "notitle.org": _excerpt(None)}
    assert gn._excerpt_title(excerpts, "absent.org", 120) is None
    assert gn._excerpt_title(excerpts, "nulled.org", 120) is None
    assert gn._excerpt_title(excerpts, "notitle.org", 120) is None
    assert gn._excerpt_title(None, "anything.org", 120) is None


def test_excerpt_title_ignores_corrupt_entry_type():
    assert gn._excerpt_title({"a.org": "just a string"}, "a.org", 120) is None


@pytest.mark.parametrize(
    "junk",
    ["Page not found", "404 Not Found", "Home", "Untitled", "Coming Soon",
     "Welcome to nginx!", "page not found."],
)
def test_excerpt_title_drops_boilerplate_titles(junk):
    """A capture that landed on a 404 or a parking page tells the reader
    nothing about what the site WAS, and 'Archived page title: Page not
    found' reads as filler on the one line that is supposed to be evidence."""
    assert gn._excerpt_title({"a.org": _excerpt(junk)}, "a.org", 120) is None


def test_excerpt_title_drops_title_that_is_just_the_domain():
    assert gn._excerpt_title(
        {"marketglow.com": _excerpt("marketglow.com")}, "marketglow.com", 120,
    ) is None
    assert gn._excerpt_title(
        {"marketglow.com": _excerpt("MarketGlow")}, "marketglow.com", 120,
    ) is None


def test_excerpt_title_denylist_is_overridable():
    """An empty denylist shows every title — the knob Mario can flip if the
    built-in list ever eats something real."""
    excerpts = {"a.org": _excerpt("Home")}
    assert gn._excerpt_title(excerpts, "a.org", 120, frozenset()) == "Home"


def test_generate_newsletter_honours_config_denylist_override():
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    # Config says "deny nothing" → the boilerplate title ships.
    cfg = _config(excerpt_title_denylist=[])
    monkey_excerpts = {"a.org": _excerpt("Home")}
    with patch.object(gn, "_load_sidecar_excerpts", return_value=monkey_excerpts):
        gn.generate_newsletter(
            cfg, {"domains": [_domain("a.org", 80)]},
            api_key="KEY", today=date(2026, 9, 19), session=session,
        )
    assert "Archived page title:" in captured["body"]
    assert "Home" in captured["body"]


def test_build_html_body_renders_archived_title_for_featured_pick():
    excerpts = {"marketglow.com": _excerpt("Celebrity Net Worth Tracker")}
    body = gn.build_html_body(
        [_domain("marketglow.com", 80)], date(2026, 9, 19), "x",
        featured_n=1, excerpts=excerpts,
    )
    assert "Archived page title:" in body
    assert "Celebrity Net Worth Tracker" in body


def test_build_html_body_renders_archived_title_in_compact_row():
    excerpts = {"tideblock.io": _excerpt("Tide tables and surf reports")}
    body = gn.build_html_body(
        [_domain("tideblock.io", 60)], date(2026, 9, 19), "x", excerpts=excerpts,
    )
    assert "Tide tables and surf reports" in body
    assert "was" in body


def test_build_html_body_no_excerpt_section_when_sidecar_empty():
    body = gn.build_html_body(
        [_domain("a.org", 80)], date(2026, 9, 19), "x", featured_n=1, excerpts={},
    )
    assert "Archived page title" not in body


def test_build_html_body_escapes_hostile_excerpt_title():
    """Archived titles are third-party spam as often as real content:
    script tags, quotes and attribute-breaking characters must not survive
    as markup, and a 5000-char title must not blow out the layout."""
    hostile = (
        '<script>alert("x")</script>" style="display:none" '
        + CJK_TITLE + f" {BIDI_OVERRIDE}evil " + "A" * 5000
    )
    excerpts = {"a.org": _excerpt(hostile)}
    body = gn.build_html_body(
        [_domain("a.org", 80)], date(2026, 9, 19), "x",
        featured_n=1, excerpts=excerpts, excerpt_max_chars=120,
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    # No raw double quote escapes the span; html.escape turns it into &quot;.
    assert '" style="display:none"' not in body
    # Bidi override stripped, CJK preserved, length capped.
    assert BIDI_OVERRIDE not in body
    assert CJK_TITLE in body
    assert "A" * 200 not in body


def test_build_html_body_caps_compact_excerpt_shorter_than_featured():
    """Compact rows share one line with the reason, so their archived title
    is cut shorter than a featured block's."""
    excerpts = {"a.org": _excerpt("B" * 400)}
    body = gn.build_html_body(
        [_domain("a.org", 80)], date(2026, 9, 19), "x",
        excerpts=excerpts, compact_excerpt_max_chars=40,
    )
    assert "B" * 39 in body
    assert "B" * 41 not in body


def test_load_sidecar_excerpts_missing_file_returns_empty(tmp_path):
    assert gn._load_sidecar_excerpts(tmp_path / "nope.json") == {}


def test_load_sidecar_excerpts_corrupt_json_returns_empty(tmp_path):
    p = tmp_path / "excerpts.json"
    p.write_text("{not json", encoding="utf-8")
    assert gn._load_sidecar_excerpts(p) == {}


def test_load_sidecar_excerpts_non_dict_returns_empty(tmp_path):
    p = tmp_path / "excerpts.json"
    p.write_text('["a.org"]', encoding="utf-8")
    assert gn._load_sidecar_excerpts(p) == {}


def test_load_sidecar_excerpts_reads_map(tmp_path):
    p = tmp_path / "excerpts.json"
    p.write_text(
        json.dumps({"a.org": _excerpt("Some title"), "b.org": None}),
        encoding="utf-8",
    )
    out = gn._load_sidecar_excerpts(p)
    assert out["a.org"]["title"] == "Some title"
    assert out["b.org"] is None


# --- Deep links to per-domain pages ------------------------------------------


def test_domain_url_deep_links_when_archived():
    url = gn._domain_url(
        "marketglow.com", "https://domainsifter.com", {"marketglow.com"},
    )
    assert url == "https://domainsifter.com/d/marketglow.com"


def test_domain_url_falls_back_to_homepage_anchor_when_not_archived():
    url = gn._domain_url(
        "tideblock.io", "https://domainsifter.com", {"marketglow.com"},
    )
    assert url == "https://domainsifter.com/#drop-tideblock.io"


def test_domain_url_falls_back_when_index_empty_or_none():
    assert gn._domain_url("a.org", "https://s", set()).endswith("/#drop-a.org")
    assert gn._domain_url("a.org", "https://s", None).endswith("/#drop-a.org")


def test_domain_url_refuses_unsafe_name_even_if_in_index():
    """Defence-in-depth: a name with characters that don't belong in a URL
    path never becomes a deep link, even if the index lists it."""
    bad = "evil<script>.org"
    assert gn._domain_url(bad, "https://s", {bad}).startswith("https://s/#drop-")


def test_build_html_body_uses_deep_link_only_for_archived_domains():
    body = gn.build_html_body(
        [_domain("marketglow.com", 80), _domain("tideblock.io", 70)],
        date(2026, 9, 19), "x", archived_names={"marketglow.com"},
    )
    assert 'href="https://domainsifter.com/d/marketglow.com"' in body
    assert 'href="https://domainsifter.com/#drop-tideblock.io"' in body
    assert "/d/tideblock.io" not in body


def test_load_archive_names_missing_file_returns_empty_set(tmp_path):
    assert gn._load_archive_names(tmp_path / "nope.json") == set()


def test_load_archive_names_reads_entry_names(tmp_path):
    p = tmp_path / "archive-index.json"
    p.write_text(
        json.dumps({
            "generated_at": "2026-09-19T00:00:00Z",
            "entries": [
                {"name": "marketglow.com", "score": 70},
                {"name": "coppernest.org", "score": 65},
                {"score": 60},            # nameless entry ignored
                "junk",                   # non-dict ignored
            ],
        }),
        encoding="utf-8",
    )
    assert gn._load_archive_names(p) == {"marketglow.com", "coppernest.org"}


def test_load_archive_names_corrupt_or_shapeless_returns_empty(tmp_path):
    bad_json = tmp_path / "a.json"
    bad_json.write_text("{nope", encoding="utf-8")
    assert gn._load_archive_names(bad_json) == set()
    shapeless = tmp_path / "b.json"
    shapeless.write_text(json.dumps({"generated_at": "x"}), encoding="utf-8")
    assert gn._load_archive_names(shapeless) == set()


# --- Featured / compact split ------------------------------------------------


def test_build_html_body_splits_featured_and_compact():
    domains = [_domain(f"d{i:02d}.org", 100 - i) for i in range(20)]
    body = gn.build_html_body(domains, date(2026, 9, 19), "x", featured_n=3)
    assert "Today's top 3" in body
    assert "The rest of today's picks" in body
    # 17 compact rows (one ds-register-cell <td> each) + 1 <th> header.
    assert body.count('td class="ds-register-cell"') == 17
    assert body.count('th class="ds-register-cell"') == 1
    # Featured picks render their own logo strips: 3 blocks x 3 logos.
    assert body.count("registrar-logos/") == 20 * 3


def test_build_html_body_featured_zero_keeps_plain_table():
    """The renderer's default is the old table - production passes
    config.newsletter.featured_n explicitly."""
    domains = [_domain(f"d{i}.org", 100 - i) for i in range(5)]
    body = gn.build_html_body(domains, date(2026, 9, 19), "x")
    assert "Today's top" not in body
    assert body.count('td class="ds-register-cell"') == 5


def test_build_html_body_all_featured_drops_empty_table():
    """featured_n >= len(domains) - no lone table header floating below."""
    domains = [_domain("a.org", 80), _domain("b.org", 70)]
    body = gn.build_html_body(domains, date(2026, 9, 19), "x", featured_n=3)
    assert "Today's top 2" in body
    assert "<th " not in body
    assert "a.org" in body and "b.org" in body


def test_build_html_body_featured_block_shows_signals():
    body = gn.build_html_body(
        [_domain("a.org", 80, wayback=766, opr=1.42, backlinks=3818)],
        date(2026, 9, 19), "x", featured_n=1,
    )
    assert "Wayback 766" in body
    assert "OPR 1.4" in body
    assert "Backlinks 3,818" in body
    assert "Score 80" in body


def test_build_html_body_under_gmail_clip_limit_with_featured_and_evidence():
    """Byte-budget regression with the richest realistic payload: 20 domains,
    3 featured, a reason on every pick and a long archived title on every
    pick. Gmail clips over ~102 KB."""
    domains = []
    excerpts = {}
    for i in range(20):
        d = _domain(f"sample{i:02d}.org", 80 - i)
        d["phase2_reason"] = "clear compound word, brandable and memorable"
        domains.append(d)
        excerpts[d["name"]] = _excerpt(
            "An archived page title of realistic length " * 3
        )
    body = gn.build_html_body(
        domains, date(2026, 9, 19), "Intro paragraph for the byte budget test.",
        featured_n=3, excerpts=excerpts,
        archived_names={d["name"] for d in domains},
        provenance="Evaluated 222,155 candidates today; 270 made the published "
                   "list, 56 of them dropped today. The 20 below are the "
                   "highest-scoring of those fresh drops.",
    )
    size = len(body.encode("utf-8"))
    assert size < 90_000, f"Body is {size} bytes; the ceiling is 90 KB."


# --- Credibility line --------------------------------------------------------


def _stats_payload(**overrides: Any) -> dict:
    base = {
        "total_candidates_evaluated": 222155,
        "domain_count": 270,
        "today_count": 56,
        "carryover_count": 214,
    }
    base.update(overrides)
    return base


def test_credibility_line_uses_only_payload_numbers():
    import re

    line = gn.credibility_line(_stats_payload(), 20)
    assert "222,155" in line
    assert "270" in line
    assert "56" in line
    assert "20" in line
    # Nothing invented: every digit group in the sentence is one of the four.
    numbers = {n.replace(",", "") for n in re.findall(r"\d[\d,]*", line)}
    assert numbers == {"222155", "270", "56", "20"}


@pytest.mark.parametrize(
    "missing",
    ["total_candidates_evaluated", "domain_count", "today_count"],
)
def test_credibility_line_omitted_when_any_field_missing(missing):
    """Hard rule 2: an email with no provenance line is fine; an email with
    a guessed one is not."""
    payload = _stats_payload()
    del payload[missing]
    assert gn.credibility_line(payload, 20) is None


@pytest.mark.parametrize("bad", [None, "1000", 12.5, True, -5])
def test_credibility_line_omitted_for_non_integer_counts(bad):
    assert gn.credibility_line(_stats_payload(domain_count=bad), 20) is None


def test_credibility_line_omitted_when_no_picks():
    assert gn.credibility_line(_stats_payload(), 0) is None


def test_build_html_body_renders_provenance_line_when_given():
    body = gn.build_html_body(
        [_domain("a.org", 80)], date(2026, 9, 19), "x",
        provenance="Evaluated 222,155 candidates today.",
    )
    assert "Evaluated 222,155 candidates today." in body


def test_build_html_body_omits_provenance_paragraph_when_none():
    body = gn.build_html_body([_domain("a.org", 80)], date(2026, 9, 19), "x")
    assert "Evaluated" not in body


def test_generate_newsletter_omits_credibility_line_when_payload_lacks_counts():
    """End-to-end: a payload with domains but no top-level counts (sample
    data, legacy JSON) ships without the line rather than with a guess."""
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    gn.generate_newsletter(
        _config(), {"domains": [_domain("a.org", 80)]},
        api_key="KEY", today=date(2026, 9, 19), session=session,
    )
    assert "Evaluated" not in captured["body"]
    assert "made the published list" not in captured["body"]


def test_generate_newsletter_includes_credibility_line_with_real_counts():
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    payload = _stats_payload()
    payload["domains"] = [_domain("a.org", 80), _domain("b.org", 70)]
    gn.generate_newsletter(
        _config(), payload,
        api_key="KEY", today=date(2026, 9, 19), session=session,
    )
    assert "Evaluated 222,155 candidates today" in captured["body"]
    assert "270 made the published list" in captured["body"]
    assert "56 of them dropped today" in captured["body"]
    assert "The 2 below" in captured["body"]


# --- Plain-text alternative part ---------------------------------------------


def test_build_text_body_contains_every_pick():
    domains = [_domain(f"d{i:02d}.org", 100 - i) for i in range(20)]
    text = gn.build_text_body(domains, date(2026, 9, 19), "Intro.", featured_n=3)
    for d in domains:
        assert d["name"] in text
    assert "<" not in text and ">" not in text  # not stripped HTML


def test_build_text_body_carries_evidence_and_unsubscribe():
    d = _domain("marketglow.com", 80)
    d["phase2_reason"] = "clear compound word, brandable"
    text = gn.build_text_body(
        [d], date(2026, 9, 19), "Intro.",
        featured_n=1,
        excerpts={"marketglow.com": _excerpt("Celebrity Net Worth Tracker")},
        archived_names={"marketglow.com"},
        provenance="Evaluated 222,155 candidates today.",
    )
    assert "DomainSifter daily picks" in text
    assert "September 19, 2026" in text
    assert "Evaluated 222,155 candidates today." in text
    assert "clear compound word, brandable" in text
    assert 'Archived page title: "Celebrity Net Worth Tracker"' in text
    assert "https://domainsifter.com/d/marketglow.com" in text
    assert "{{ unsubscribe_url }}" in text


def test_build_text_body_cleans_hostile_excerpt():
    """Same untrusted-text handling as the HTML part: capped, control and
    bidi characters gone, and never a newline that could fake a new pick."""
    hostile = f"line1\nline2{BIDI_OVERRIDE} " + "C" * 5000
    text = gn.build_text_body(
        [_domain("a.org", 80)], date(2026, 9, 19), "Intro.",
        featured_n=1, excerpts={"a.org": _excerpt(hostile)},
        excerpt_max_chars=100,
    )
    title_lines = [l for l in text.splitlines() if "Archived page title" in l]
    assert len(title_lines) == 1
    assert BIDI_OVERRIDE not in title_lines[0]
    assert len(title_lines[0]) < 160
    assert "line1 line2" in title_lines[0]


def test_build_text_body_has_no_registrar_tracking_urls():
    """The text part deliberately carries no affiliate URLs - 60 tracking
    links would dominate it and raise URL-density spam signals."""
    text = gn.build_text_body(
        [_domain("a.org", 80)], date(2026, 9, 19), "Intro.", featured_n=1,
    )
    assert "utm_source" not in text
    assert "namecheap" not in text.lower()


def test_build_text_body_omits_missing_reason_and_title_lines():
    text = gn.build_text_body(
        [_domain("bare.org", 80)], date(2026, 9, 19), "Intro.", featured_n=1,
    )
    assert "Archived page title" not in text
    body_section = text.split("TODAY'S TOP")[1].split("Full daily list")[0]
    assert "—" not in body_section


def test_generate_newsletter_returns_text_body_in_dry_run():
    out = gn.generate_newsletter(
        _config(), {"domains": [_domain("a.org", 80)]},
        api_key="KEY", today=date(2026, 9, 19), dry_run=True,
    )
    assert out["status"] == "dry_run"
    assert "a.org" in out["text_body"]
    assert out["text_chars"] > 0


def test_create_draft_omits_plaintext_field_by_default():
    """Unconfigured - byte-identical payload to the pre-2026-09-19 one."""
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "x"}
        return resp

    session = MagicMock()
    session.post.side_effect = post_capture
    gn._create_draft("KEY", "S", "<html>", text_body="plain text", session=session)
    assert set(captured["json"]) == {"subject", "body", "status"}


def test_create_draft_sends_plaintext_field_when_configured():
    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "x"}
        return resp

    session = MagicMock()
    session.post.side_effect = post_capture
    gn._create_draft(
        "KEY", "S", "<html>",
        text_body="plain text", plaintext_field="body_plaintext",
        session=session,
    )
    assert captured["json"]["body_plaintext"] == "plain text"


def test_create_draft_retries_without_plaintext_field_on_4xx():
    """A wrong field name must degrade to the old payload, not cost the
    draft - the send is the point, the text part is the bonus."""
    calls: list[dict] = []

    def post_capture(url, headers=None, json=None, timeout=None):
        calls.append(dict(json))  # copy: the retry mutates the same dict
        resp = MagicMock()
        if len(calls) == 1:
            resp.status_code = 422
            resp.text = "unknown field"
        else:
            resp.status_code = 201
            resp.json.return_value = {"id": "recovered"}
        return resp

    session = MagicMock()
    session.post.side_effect = post_capture
    out = gn._create_draft(
        "KEY", "S", "<html>",
        text_body="plain text", plaintext_field="wrong_field",
        session=session,
    )
    assert out["id"] == "recovered"
    assert "wrong_field" in calls[0]
    assert "wrong_field" not in calls[1]


def test_create_draft_does_not_retry_on_5xx():
    """A server error is not a payload problem; retrying without the field
    would just double the load on a struggling API."""
    session = _fake_session([
        {"method": "POST", "status": 500, "json": None, "text": "boom"},
    ])
    with pytest.raises(gn.ButtondownError, match="HTTP 500"):
        gn._create_draft(
            "KEY", "S", "<html>",
            text_body="plain", plaintext_field="body_plaintext",
            session=session,
        )


# --- End-to-end evidence wiring ---------------------------------------------


def test_generate_newsletter_wires_sidecars_from_config(tmp_path):
    """Config-supplied sidecar paths are read, and their content reaches the
    draft body: archived title + deep link for the domain that has both."""
    excerpts_path = tmp_path / "wayback_excerpts.json"
    excerpts_path.write_text(
        json.dumps({"marketglow.com": _excerpt("Celebrity Net Worth Tracker")}),
        encoding="utf-8",
    )
    index_path = tmp_path / "archive-index.json"
    index_path.write_text(
        json.dumps({"entries": [{"name": "marketglow.com"}]}), encoding="utf-8",
    )

    captured: dict = {}

    def post_capture(url, headers=None, json=None, timeout=None):
        captured["body"] = json["body"]
        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "id", "subject": json["subject"]}
        return resp

    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
    ])
    session.post.side_effect = post_capture

    cfg = _config(
        featured_n=1,
        sidecar_excerpts_path=str(excerpts_path),
        archive_index_path=str(index_path),
    )
    first = _domain("marketglow.com", 90)
    first["phase2_reason"] = "clear compound word, brandable"
    out = gn.generate_newsletter(
        cfg, {"domains": [first, _domain("tideblock.io", 70)]},
        api_key="KEY", today=date(2026, 9, 19), session=session,
    )
    assert out["status"] == "created"
    assert "Celebrity Net Worth Tracker" in captured["body"]
    assert "clear compound word, brandable" in captured["body"]
    assert 'href="https://domainsifter.com/d/marketglow.com"' in captured["body"]
    assert "/d/tideblock.io" not in captured["body"]


def test_generate_newsletter_survives_missing_sidecars(tmp_path):
    """Both sidecars absent - draft still created, just without archived
    titles and deep links. Evidence is never worth losing a send over."""
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {"results": [], "next": None}},
        {"method": "POST", "status": 201, "json": {"id": "id", "subject": "s"}},
    ])
    cfg = _config(
        sidecar_excerpts_path=str(tmp_path / "missing_excerpts.json"),
        archive_index_path=str(tmp_path / "missing_index.json"),
    )
    out = gn.generate_newsletter(
        cfg, {"domains": [_domain("a.org", 80)]},
        api_key="KEY", today=date(2026, 9, 19), session=session,
    )
    assert out["status"] == "created"


def test_generate_newsletter_idempotency_unaffected_by_evidence(tmp_path):
    """The 2026-09-19 evidence work must not touch the same-day skip: a
    matching subject still short-circuits before any POST."""
    excerpts_path = tmp_path / "wayback_excerpts.json"
    excerpts_path.write_text(
        json.dumps({"a.org": _excerpt("Some archived title")}), encoding="utf-8",
    )
    subject = "DomainSifter daily picks — September 19, 2026"
    session = _fake_session([
        {"method": "GET", "status": 200, "json": {
            "results": [{"id": "existing", "subject": subject}], "next": None,
        }},
    ])
    cfg = _config(featured_n=3, sidecar_excerpts_path=str(excerpts_path))
    out = gn.generate_newsletter(
        cfg, {"domains": [_domain("a.org", 80)]},
        api_key="KEY", today=date(2026, 9, 19), session=session,
    )
    assert out["status"] == "skipped_duplicate"
    assert out["id"] == "existing"
    session.post.assert_not_called()


def test_main_dry_run_text_flag_prints_plain_text(tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_config(featured_n=1)), encoding="utf-8")
    input_path = tmp_path / "daily.json"
    input_path.write_text(
        json.dumps({"domains": [_domain("a.org", 80)]}), encoding="utf-8",
    )
    rc = gn.main([
        "--config", str(cfg_path), "--input", str(input_path),
        "--dry-run", "--text",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<!DOCTYPE html>" not in out
    assert "a.org" in out
    assert "{{ unsubscribe_url }}" in out
