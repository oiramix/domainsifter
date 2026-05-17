"""Wayback excerpt fetcher for the archive subsystem.

Separate from scripts/enrichment/wayback.py because:
  - That module is the pipeline's enrichment phase (snapshot count +
    last-date for scoring); this is the archive subsystem (content
    excerpt for the per-page "Historical use" subsection).
  - Different output shape: enrichment returns scoring fields; this
    returns a content dict or None.
  - Different rate-limit posture: the pipeline batches thousands of
    candidates against the CDX API; archive runs ~30-50 calls/day
    against the Availability API + the per-snapshot HTML at a slow
    1 req/sec pace (the archive_generator sleeps between calls).

Public surface (one function):

    fetch_excerpt(domain, target_date) -> dict | None

Flow:
    1. Wayback Availability API → closest snapshot URL + timestamp.
    2. Fetch the snapshot HTML.
    3. Parse with BeautifulSoup ('html.parser', stdlib — no lxml dep).
    4. Extract title, meta description, h1[:3], h2[:5]. Cap each
       string at 200 chars so a keyword-stuffed parked-page title can't
       blow the prompt budget.
    5. Return None when any step fails OR when no content signal
       survives extraction.

User-Agent identifies the crawler. archive.org doesn't formally
publish a rate limit; the surrounding `time.sleep(1.0)` in
archive_generator's per-domain loop keeps us well inside organic-user
territory.
"""

from __future__ import annotations

import logging

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = "DomainSifter archive-generator/1.0 (+https://domainsifter.com)"
AVAILABILITY_API_URL = "https://archive.org/wayback/available"
AVAILABILITY_TIMEOUT_SECONDS = 10
SNAPSHOT_TIMEOUT_SECONDS = 15
TITLE_MAX_CHARS = 200
META_DESCRIPTION_MAX_CHARS = 200
H1_MAX_COUNT = 3
H1_MAX_CHARS = 200
H2_MAX_COUNT = 5
H2_MAX_CHARS = 200


# --- Helpers ----------------------------------------------------------------


def _normalize_date_for_availability(target_date: str) -> str:
    """Convert 'YYYY-MM-DD' to the 'YYYYMMDD' the Availability API expects.
    Defensive — strips any extra separators in case the input format drifts.
    """
    return target_date.replace("-", "")


def _truncate(value: str | None, max_chars: int) -> str | None:
    """Strip, cap at max_chars, return None for empty result. Used for the
    title and meta description fields so a 4 KB keyword-stuffed title from
    a parked page can't dominate the prompt."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_chars]


def _fetch_availability(domain: str, target_date: str) -> dict | None:
    """Hit the Availability API. Returns parsed JSON or None on any failure
    (network, non-200, non-JSON)."""
    try:
        resp = requests.get(
            AVAILABILITY_API_URL,
            params={
                "url": domain,
                "timestamp": _normalize_date_for_availability(target_date),
            },
            headers={"User-Agent": USER_AGENT},
            timeout=AVAILABILITY_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.debug("Availability API error for %s: %s", domain, exc)
        return None
    if resp.status_code != 200:
        logger.debug(
            "Availability API non-200 for %s: HTTP %d",
            domain, resp.status_code,
        )
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _extract_closest_snapshot(payload: dict | None) -> dict | None:
    """Pull the closest-snapshot subdict from an Availability response.
    Returns None when the payload lacks a usable snapshot reference."""
    if not payload:
        return None
    closest = ((payload.get("archived_snapshots") or {}).get("closest")) or None
    if not closest:
        return None
    # 'available' is a bool flag the API sets explicitly. False means
    # archived_snapshots existed but the closest match is unreachable.
    if not closest.get("available", False):
        return None
    if not closest.get("url"):
        return None
    return closest


def _fetch_snapshot_html(snapshot_url: str) -> bytes | None:
    """GET the snapshot, follow Wayback's internal redirects. Returns the
    raw response body as BYTES (not decoded text) or None on any failure
    / empty body.

    Why bytes, not text: many Wayback snapshots are served without a
    Content-Type charset, so requests.text defaults to ISO-8859-1
    decoding. If the page is actually UTF-8 (common for non-English
    sites), text content inside tags later gets re-decoded by bs4 via
    the page's own `<meta charset>` tag, but ATTRIBUTE values like
    `<meta name="description" content="...">` aren't re-decoded — they
    stay as Latin-1 mojibake. Empirically observed on waterangels.net
    during the 2026-05-18 grounding rollout: the Chinese title parsed
    correctly while the meta description came back as garbage. Fix is
    to defer all decoding to bs4 (UnicodeDammit), which sniffs both the
    HTTP header AND the document's own charset declaration."""
    try:
        resp = requests.get(
            snapshot_url,
            headers={"User-Agent": USER_AGENT},
            timeout=SNAPSHOT_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        logger.debug("Snapshot fetch error for %s: %s", snapshot_url, exc)
        return None
    if resp.status_code != 200:
        logger.debug(
            "Snapshot fetch non-200 for %s: HTTP %d",
            snapshot_url, resp.status_code,
        )
        return None
    if not resp.content:
        return None
    return resp.content


def _parse_content_signals(html: str) -> dict:
    """Extract the four content fields from a snapshot's HTML. All four
    can be None / empty independently — caller (fetch_excerpt) decides
    whether the overall combination is usable."""
    soup = BeautifulSoup(html, "html.parser")

    # <title> — soup.title.string can be None when the tag is empty or
    # contains nested elements; fall back to get_text() in that case.
    title: str | None = None
    if soup.title is not None:
        title_text = soup.title.string
        if title_text is None:
            title_text = soup.title.get_text()
        title = _truncate(title_text, TITLE_MAX_CHARS)

    # <meta name="description" content="..."> — case-insensitive on the
    # name attribute because real-world HTML capitalises it both ways
    # (and bs4's attrs= dict match is case-sensitive by default).
    meta_description: str | None = None
    meta_tag = soup.find(
        "meta",
        attrs={"name": lambda v: bool(v) and v.lower() == "description"},
    )
    if meta_tag is not None:
        content_value = meta_tag.get("content")
        if content_value:
            meta_description = _truncate(content_value, META_DESCRIPTION_MAX_CHARS)

    # h1 / h2 — cap counts then cap per-element chars. Skip elements
    # whose stripped text is empty (decorative SVG-only headings etc.).
    h1: list[str] = []
    for tag in soup.find_all("h1")[:H1_MAX_COUNT]:
        text = tag.get_text(strip=True)
        if text:
            h1.append(text[:H1_MAX_CHARS])

    h2: list[str] = []
    for tag in soup.find_all("h2")[:H2_MAX_COUNT]:
        text = tag.get_text(strip=True)
        if text:
            h2.append(text[:H2_MAX_CHARS])

    return {
        "title": title,
        "meta_description": meta_description,
        "h1": h1,
        "h2": h2,
    }


# --- Public API -------------------------------------------------------------


def fetch_excerpt(domain: str, target_date: str) -> dict | None:
    """Fetch the closest Wayback snapshot to `target_date` for `domain`
    and return a content excerpt for archive-page grounding.

    Args:
        domain: bare apex like "deepsand.net".
        target_date: "YYYY-MM-DD" — matches the pipeline's
            wayback_last_snapshot field exactly.

    Returns:
        {
            "snapshot_timestamp": "20251215123456",   # 14-digit Wayback ts
            "snapshot_url":       "http://web.archive.org/web/.../http://deepsand.net/",
            "title":              str | None,
            "meta_description":   str | None,
            "h1":                 list[str],   # up to 3, each ≤ 200 chars
            "h2":                 list[str],   # up to 5, each ≤ 200 chars
        }

        OR None when any of:
          - Availability API errors / returns no snapshot
          - "closest.available" is false or "url" missing
          - Snapshot HTML fetch errors / non-200 / empty body
          - All four content signals are empty after parsing

    Never raises on network failure — caller treats None as "no
    grounding available, omit the Historical use subsection."
    """
    availability_payload = _fetch_availability(domain, target_date)
    closest = _extract_closest_snapshot(availability_payload)
    if not closest:
        return None

    html = _fetch_snapshot_html(closest["url"])
    if not html:
        return None

    signals = _parse_content_signals(html)
    if (
        not signals["title"]
        and not signals["meta_description"]
        and not signals["h1"]
        and not signals["h2"]
    ):
        return None

    return {
        "snapshot_timestamp": closest.get("timestamp"),
        "snapshot_url": closest["url"],
        **signals,
    }
