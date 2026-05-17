"""Unit tests for scripts/wayback_excerpt.py.

All HTTP mocked via monkeypatching the module-level `requests.get` — no
live calls to archive.org. The module is small and pure-ish (one
external dep: requests), so manual monkeypatching keeps the test deps
flat (no requests_mock / responses) and reads simply.

What's covered:
  - happy paths (full / partial signal sets)
  - Availability API failure modes (4xx, 5xx, timeout, no closest,
    available=false, missing url)
  - snapshot HTML failure modes (4xx, timeout, empty body)
  - parse outcomes (no usable signals → None, truncation, count caps,
    case-insensitive meta-name match, parked-page passthrough)

What's not covered here:
  - The archive_generator wiring (tested in test_archive_generator.py
    via stubbed clients).
  - The live Availability API surface — we treat its documented JSON
    shape as the contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from scripts import wayback_excerpt as we


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _resp(
    status: int = 200,
    *,
    json_data: Any = None,
    text: str = "",
    content: bytes | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like requests.Response.

    `text` is kept for backwards-readability of the existing tests; when
    only `text=` is passed it's also exposed as utf-8 bytes via .content
    so the snapshot-fetch path (which now reads .content to preserve the
    raw bytes for bs4's UnicodeDammit) sees the same payload."""
    r = MagicMock()
    r.status_code = status
    if json_data is None:
        r.json.side_effect = ValueError("no body")
    else:
        r.json.return_value = json_data
    r.text = text
    r.content = content if content is not None else text.encode("utf-8")
    return r


def _avail_payload(*, available: bool = True, url: str = "http://web.archive.org/web/20251215120000/http://example.com/",
                   timestamp: str = "20251215120000") -> dict:
    """Realistic Availability-API JSON shape."""
    if not available:
        return {"archived_snapshots": {"closest": {"available": False}}}
    return {
        "url": "example.com",
        "archived_snapshots": {
            "closest": {
                "status": "200",
                "available": True,
                "url": url,
                "timestamp": timestamp,
            },
        },
    }


def _install_get_sequence(monkeypatch, responses: list):
    """Queue `responses` (Mocks or exceptions). Each `requests.get` call
    pops the next; raises if a Mock was provided as an exception."""
    queue = list(responses)

    def fake_get(*args, **kwargs):
        if not queue:
            raise AssertionError("unexpected requests.get call; queue empty")
        next_item = queue.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    monkeypatch.setattr(we.requests, "get", fake_get)


# ---------------------------------------------------------------------------
# Helpers — pure
# ---------------------------------------------------------------------------


def test_normalize_date_strips_hyphens():
    assert we._normalize_date_for_availability("2025-12-15") == "20251215"


def test_truncate_returns_none_for_empty():
    assert we._truncate("", 100) is None
    assert we._truncate("   ", 100) is None
    assert we._truncate(None, 100) is None


def test_truncate_caps_at_max_chars():
    assert we._truncate("a" * 500, 200) == "a" * 200


def test_truncate_strips_whitespace():
    assert we._truncate("  hello world  ", 100) == "hello world"


# ---------------------------------------------------------------------------
# Availability API — failure modes
# ---------------------------------------------------------------------------


def test_availability_returns_none_on_timeout(monkeypatch):
    _install_get_sequence(monkeypatch, [requests.Timeout("avail timed out")])
    assert we._fetch_availability("ex.com", "2025-12-15") is None


def test_availability_returns_none_on_connection_error(monkeypatch):
    _install_get_sequence(monkeypatch, [requests.ConnectionError("refused")])
    assert we._fetch_availability("ex.com", "2025-12-15") is None


def test_availability_returns_none_on_404(monkeypatch):
    _install_get_sequence(monkeypatch, [_resp(404, text="not found")])
    assert we._fetch_availability("ex.com", "2025-12-15") is None


def test_availability_returns_none_on_5xx(monkeypatch):
    _install_get_sequence(monkeypatch, [_resp(503, text="overloaded")])
    assert we._fetch_availability("ex.com", "2025-12-15") is None


def test_availability_returns_none_on_non_json(monkeypatch):
    _install_get_sequence(monkeypatch, [_resp(200, text="<html>not json</html>")])
    assert we._fetch_availability("ex.com", "2025-12-15") is None


# ---------------------------------------------------------------------------
# _extract_closest_snapshot — payload shape gates
# ---------------------------------------------------------------------------


def test_extract_returns_none_for_empty_archived_snapshots():
    """Real Availability response when there are NO snapshots for a domain:
    {"archived_snapshots": {}}.
    """
    assert we._extract_closest_snapshot({"archived_snapshots": {}}) is None


def test_extract_returns_none_when_available_false():
    payload = _avail_payload(available=False)
    assert we._extract_closest_snapshot(payload) is None


def test_extract_returns_none_when_url_missing():
    payload = {
        "archived_snapshots": {
            "closest": {"available": True, "timestamp": "20251215000000"},
            # url missing
        },
    }
    assert we._extract_closest_snapshot(payload) is None


def test_extract_returns_closest_on_happy_payload():
    closest = we._extract_closest_snapshot(_avail_payload())
    assert closest is not None
    assert closest["url"].startswith("http://web.archive.org/web/")


# ---------------------------------------------------------------------------
# _fetch_snapshot_html — failure modes
# ---------------------------------------------------------------------------


def test_snapshot_fetch_returns_none_on_404(monkeypatch):
    _install_get_sequence(monkeypatch, [_resp(404, text="gone")])
    assert we._fetch_snapshot_html("http://web.archive.org/web/x/y") is None


def test_snapshot_fetch_returns_none_on_empty_body(monkeypatch):
    _install_get_sequence(monkeypatch, [_resp(200, text="")])
    assert we._fetch_snapshot_html("http://web.archive.org/web/x/y") is None


def test_snapshot_fetch_returns_none_on_timeout(monkeypatch):
    _install_get_sequence(monkeypatch, [requests.Timeout("snap timed out")])
    assert we._fetch_snapshot_html("http://web.archive.org/web/x/y") is None


# ---------------------------------------------------------------------------
# _parse_content_signals — bs4 extraction
# ---------------------------------------------------------------------------


def test_parse_extracts_title_meta_h1_h2():
    html = """
    <html><head>
      <title>Deep Sand Conservation Project</title>
      <meta name="description" content="Restoring desert ecosystems since 2008.">
    </head><body>
      <h1>Welcome to Deep Sand</h1>
      <h2>Our Mission</h2>
      <h2>Recent Work</h2>
    </body></html>
    """
    out = we._parse_content_signals(html)
    assert out["title"] == "Deep Sand Conservation Project"
    assert out["meta_description"] == "Restoring desert ecosystems since 2008."
    assert out["h1"] == ["Welcome to Deep Sand"]
    assert out["h2"] == ["Our Mission", "Recent Work"]


def test_parse_caps_h1_count_at_three():
    html = "<html><body>" + "".join(f"<h1>h{i}</h1>" for i in range(10)) + "</body></html>"
    out = we._parse_content_signals(html)
    assert out["h1"] == ["h0", "h1", "h2"]


def test_parse_caps_h2_count_at_five():
    html = "<html><body>" + "".join(f"<h2>h{i}</h2>" for i in range(10)) + "</body></html>"
    out = we._parse_content_signals(html)
    assert out["h2"] == ["h0", "h1", "h2", "h3", "h4"]


def test_parse_truncates_title_to_200_chars():
    long = "a" * 500
    html = f"<html><head><title>{long}</title></head><body></body></html>"
    out = we._parse_content_signals(html)
    assert len(out["title"]) == 200


def test_parse_truncates_each_h1_to_200_chars():
    long = "b" * 500
    html = f"<html><body><h1>{long}</h1></body></html>"
    out = we._parse_content_signals(html)
    assert len(out["h1"][0]) == 200


def test_parse_meta_description_case_insensitive_name_match():
    """Real-world HTML capitalizes 'Description' both ways; bs4's attrs=
    dict match is case-sensitive by default, hence the lambda."""
    for name_attr in ("description", "Description", "DESCRIPTION", "DeScRiPtIoN"):
        html = f'<html><head><meta name="{name_attr}" content="hit"></head><body></body></html>'
        out = we._parse_content_signals(html)
        assert out["meta_description"] == "hit", f"failed for name={name_attr!r}"


def test_parse_skips_empty_headings():
    html = "<html><body><h1></h1><h1>  </h1><h1>real</h1></body></html>"
    out = we._parse_content_signals(html)
    assert out["h1"] == ["real"]


def test_parse_no_signals_returns_empty_dict():
    html = "<html><body><p>just a paragraph</p></body></html>"
    out = we._parse_content_signals(html)
    assert out["title"] is None
    assert out["meta_description"] is None
    assert out["h1"] == []
    assert out["h2"] == []


def test_parse_parked_page_title_passes_through():
    """The spec is explicit: a parked / for-sale title is REAL evidence
    and should be returned. The prompt then reports it factually."""
    html = '<html><head><title>Domain for sale - Buy deepsand.net</title></head><body></body></html>'
    out = we._parse_content_signals(html)
    assert out["title"] == "Domain for sale - Buy deepsand.net"


# ---------------------------------------------------------------------------
# fetch_excerpt — end-to-end (still all mocked, two-stage queue)
# ---------------------------------------------------------------------------


def test_fetch_excerpt_happy_path(monkeypatch):
    avail = _avail_payload(
        url="http://web.archive.org/web/20251215120000/http://deepsand.net/",
        timestamp="20251215120000",
    )
    snap_html = """
    <html><head>
      <title>Deep Sand Soil Studies</title>
      <meta name="description" content="Geological research on arid soils.">
    </head><body>
      <h1>Deep Sand</h1>
      <h2>Publications</h2>
    </body></html>
    """
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=avail),
        _resp(200, text=snap_html),
    ])
    out = we.fetch_excerpt("deepsand.net", "2025-12-15")
    assert out is not None
    assert out["snapshot_timestamp"] == "20251215120000"
    assert "web.archive.org" in out["snapshot_url"]
    assert out["title"] == "Deep Sand Soil Studies"
    assert out["meta_description"] == "Geological research on arid soils."
    assert out["h1"] == ["Deep Sand"]
    assert out["h2"] == ["Publications"]


def test_fetch_excerpt_only_title_still_returned(monkeypatch):
    """Title alone is a usable signal — return it; let Haiku decide
    what to write with just a title."""
    avail = _avail_payload()
    snap_html = "<html><head><title>Only A Title</title></head><body></body></html>"
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=avail),
        _resp(200, text=snap_html),
    ])
    out = we.fetch_excerpt("ex.com", "2025-12-15")
    assert out is not None
    assert out["title"] == "Only A Title"
    assert out["meta_description"] is None
    assert out["h1"] == []


def test_fetch_excerpt_only_meta_description_still_returned(monkeypatch):
    avail = _avail_payload()
    snap_html = '<html><head><meta name="description" content="Only a description here."></head><body></body></html>'
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=avail),
        _resp(200, text=snap_html),
    ])
    out = we.fetch_excerpt("ex.com", "2025-12-15")
    assert out is not None
    assert out["title"] is None
    assert out["meta_description"] == "Only a description here."


def test_fetch_excerpt_returns_none_when_availability_empty(monkeypatch):
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data={"archived_snapshots": {}}),
    ])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_returns_none_when_available_false(monkeypatch):
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=_avail_payload(available=False)),
    ])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_returns_none_when_availability_404(monkeypatch):
    _install_get_sequence(monkeypatch, [_resp(404, text="not found")])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_returns_none_when_availability_timeout(monkeypatch):
    _install_get_sequence(monkeypatch, [requests.Timeout("avail")])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_returns_none_when_snapshot_timeout(monkeypatch):
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=_avail_payload()),
        requests.Timeout("snap"),
    ])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_returns_none_when_snapshot_404(monkeypatch):
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=_avail_payload()),
        _resp(404, text="snapshot missing"),
    ])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_returns_none_when_snapshot_empty(monkeypatch):
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=_avail_payload()),
        _resp(200, text=""),
    ])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None


def test_fetch_excerpt_decodes_utf8_meta_description_correctly(monkeypatch):
    """Regression guard for the 2026-05-18 mojibake bug observed on
    waterangels.net: title decoded as proper Chinese but meta description
    came back as Latin-1-mojibake'd UTF-8 bytes. The fix was to read
    `resp.content` (raw bytes) and let bs4's UnicodeDammit do a single
    consistent decode via the document's own <meta charset>.

    Test: page declares UTF-8 internally; the response is sent as raw
    bytes; meta description contains non-ASCII characters. After parse,
    the meta description must come out as the correct Unicode string."""
    chinese_title = "网站标题"
    chinese_desc = "这是一个中文网站的描述。"
    html_bytes = (
        f'<html><head><meta charset="utf-8">'
        f'<title>{chinese_title}</title>'
        f'<meta name="description" content="{chinese_desc}">'
        f'</head><body></body></html>'
    ).encode("utf-8")
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=_avail_payload()),
        _resp(200, content=html_bytes),
    ])
    out = we.fetch_excerpt("ex.com", "2025-12-15")
    assert out is not None
    assert out["title"] == chinese_title
    assert out["meta_description"] == chinese_desc


def test_fetch_excerpt_returns_none_when_snapshot_has_no_signals(monkeypatch):
    """HTML parsed cleanly but yielded nothing useful — caller must
    treat as 'no grounding available' and omit the Historical use section."""
    avail = _avail_payload()
    snap_html = "<html><body><p>just paragraphs, no title, no meta, no headings</p></body></html>"
    _install_get_sequence(monkeypatch, [
        _resp(200, json_data=avail),
        _resp(200, text=snap_html),
    ])
    assert we.fetch_excerpt("ex.com", "2025-12-15") is None
