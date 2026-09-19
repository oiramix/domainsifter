"""Daily newsletter draft generator — Buttondown API integration.

Reads `src/data/daily-domains.json`, filters to fresh-today entries
(days_listed == 0), takes the top N by score, builds an HTML email body,
and POSTs it to Buttondown as a draft. The draft is never auto-sent —
Mario uses Buttondown's dashboard "Send draft" to QA against his own
email, then flips status to `about_to_send` when satisfied.

Evidence wire-in (2026-09-19) — what the email now says beyond the names:
  - `phase2_reason` (per-domain, from daily-domains.json): the ranker's
    3-6 word justification. Null on mechanical-fallback days and on
    pre-2026-09-19 carryover; when it's null nothing is rendered in its
    place (no "—" filler, no empty padded element).
  - `src/data/wayback_excerpts.json` (sidecar, keyed by domain name): the
    archived <title> from the last Wayback capture — "what the site used
    to be". UNTRUSTED third-party text: frequently spam, frequently
    non-English. It is control/bidi-stripped, whitespace-collapsed,
    length-capped and HTML-escaped before it reaches the body. A missing
    or corrupt sidecar degrades silently to "no archived titles today".
  - `src/data/archive-index.json`: which domains have a permanent page at
    {site_url}/d/{name}. Deep-link the name when a page exists; fall back
    to the homepage row anchor when it doesn't.
  - Top-level payload counts (`total_candidates_evaluated`, `domain_count`,
    `today_count`): one credibility line built ONLY from those fields. If
    any of them is missing the whole line is omitted — never estimated,
    never rounded up (CLAUDE.md hard rule 2).

Layout: the first `newsletter.featured_n` picks render as rich blocks
(reason + archived title + signals + registrars); the rest stay in the
existing compact table. A flat 20-row list gets skimmed and closed.

Both a `text/html` and a `text/plain` rendering are produced from the same
data (never by stripping the HTML). Whether the plain-text part is sent to
Buttondown depends on `newsletter.plaintext_api_field` — see that key's
note in `_create_draft`.

Why filter to fresh-today (changed 2026-05-17): the pipeline writes
~150-200 domains per day — ~50-100 fresh plus the 14-day carryover. The
"Today's top expired domain picks" framing only makes sense for fresh
drops; sending top-20-by-score across the union routinely shipped 1-14-
day-old carryover under that header. Owner policy: serve only fresh, no
top-up from carryover, no minimum count floor.

Idempotency: queries Buttondown's existing drafts before creating; if a draft
whose subject matches today's exact subject line is already there, skip.
Subject line includes the ISO date, so duplicates only fire if the script
runs twice the same day.

Plumbing position:
  - Pipeline writes daily-domains.json (its primary contract).
  - run-daily.sh invokes this AFTER pipeline.py succeeds.
  - Any failure here is logged; the daily run's exit code is the pipeline's,
    not this module's. The email report still fires regardless.

CLI:
    python -m scripts.generate_newsletter
        [--config scripts/config.json]
        [--input src/data/daily-domains.json]
        [--dry-run]   # build the HTML, print to stdout, no Buttondown call
        [--text]      # with --dry-run: print the text/plain part instead

Exit codes:
    0 — draft created OR skipped (duplicate / disabled / empty / no-fresh)
    1 — config or input file missing / unreadable
    2 — Buttondown API failure

Configuration sources:
    config["newsletter"]["enabled"]           — feature flag (default False)
    config["newsletter"]["top_n"]             — default 20
    config["newsletter"]["subject_template"]  — uses {date} placeholder ({n}
                                              still accepted for back-compat)
    config["newsletter"]["intro_text"]        — body intro paragraph
    config["newsletter"]["site_url"]          — for "see full list" link
    config["newsletter"]["featured_n"]        — rich blocks at the top
                                              (default 3)
    config["newsletter"]["sidecar_excerpts_path"]
                                              — default
                                              src/data/wayback_excerpts.json
    config["newsletter"]["archive_index_path"]
                                              — default
                                              src/data/archive-index.json
    config["newsletter"]["reason_max_chars"]  — default 120
    config["newsletter"]["excerpt_max_chars"] — featured archived title cap,
                                              default 120
    config["newsletter"]["compact_excerpt_max_chars"]
                                              — compact-row archived title
                                              cap, default 70
    config["newsletter"]["excerpt_title_denylist"]
                                              — archived titles to treat as
                                              no title (404 / parking /
                                              server-default pages);
                                              omit for the built-in list,
                                              [] to show every title
    config["newsletter"]["plaintext_api_field"]
                                              — Buttondown field name for the
                                              text/plain part; "" (default)
                                              means don't send one
    env BUTTONDOWN_API_KEY                    — required when enabled=true
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

BUTTONDOWN_API_BASE = "https://api.buttondown.com/v1"
DEFAULT_SITE_URL = "https://domainsifter.com"
LOGO_URL_TEMPLATE = "{site_url}/registrar-logos/{slug}.png"

# Map config registrar name → logo filename slug. PNG assets already live at
# public/registrar-logos/{slug}.png in the repo.
REGISTRAR_LOGO_SLUGS = {
    "Namecheap": "namecheap",
    "NameSilo": "namesilo",
    "Dynadot": "dynadot",
}

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TOP_N = 20
DEFAULT_FEATURED_N = 3
DEFAULT_SIDECAR_EXCERPTS_PATH = "src/data/wayback_excerpts.json"
DEFAULT_ARCHIVE_INDEX_PATH = "src/data/archive-index.json"

# Caps on untrusted archived text and on the ranker's justification. Featured
# blocks get a full line to themselves; compact rows share one line with the
# reason, so their archived title is cut shorter.
DEFAULT_REASON_MAX_CHARS = 120
DEFAULT_EXCERPT_MAX_CHARS = 120
DEFAULT_COMPACT_EXCERPT_MAX_CHARS = 70

# The ranker writes this into phase2_reason when a name was sent to the model
# but came back absent from the reply. A pipeline marker, not a justification;
# it must never reach a subscriber. Duplicated (not imported) from
# scripts/output.py so this module stays correct against any daily-domains.json,
# including ones written before that filter existed.
PHASE2_REASON_PLACEHOLDER = "missing from response"

# Archived titles that say nothing about what the site WAS: 404s, parking
# pages and untouched server defaults. Matched case-insensitively against the
# cleaned title with trailing dots stripped. Override with
# config.newsletter.excerpt_title_denylist (set it to [] to show everything).
EXCERPT_TITLE_DENYLIST = frozenset({
    "404",
    "404 not found",
    "403 forbidden",
    "500 internal server error",
    "not found",
    "page not found",
    "error",
    "untitled",
    "untitled document",
    "home",
    "home page",
    "homepage",
    "index",
    "welcome",
    "test page",
    "under construction",
    "coming soon",
    "domain default page",
    "welcome to nginx!",
    "apache2 ubuntu default page: it works",
})

# Conservative gate on names we put into a /d/{name} URL path. Domain names
# come from the pipeline already normalised, so this is a sanity check, not a
# sanitiser — a name that fails it simply doesn't get deep-linked.
_SAFE_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.-")

DEFAULT_SUBJECT_TEMPLATE = "DomainSifter daily picks — {date}"
DEFAULT_INTRO = (
    "Today's top expired domain picks, sorted by score. Backlinks counts "
    "come from Common Crawl's last 3 months of crawl data. All domains "
    "were available at last check — verify at registrar before buying."
)
UTM_PARAMS = {
    "utm_source": "newsletter",
    "utm_medium": "email",
    "utm_campaign": "daily",
}


# --- Helpers -----------------------------------------------------------------


def _append_utm(url: str, params: dict[str, str] | None = None) -> str:
    """Append UTM (or arbitrary) params to an existing URL, preserving its
    existing query string. Each key in `params` overwrites any same-named
    existing param. Works for both pxf.io-style affiliate URLs and direct
    registrar URLs."""
    params = params if params is not None else UTM_PARAMS
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    for k, v in params.items():
        qs[k] = [v]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


def _domain_slug(name: str) -> str:
    """The per-row anchor ID matches the slug emitted by DomainTable.astro:
    `drop-{full_apex_with_dot}`. Browsers accept dots in HTML5 IDs and in
    URL fragments. Keep in sync with the id= attribute in DomainTable.astro."""
    return f"drop-{name}"


def _verdict_from_score(score: int) -> str:
    """Score-only fallback for old payloads (sample-domains.json from before
    the 2026-05-17 contract addition) that lack a server-computed verdict
    field. Production reads should hit `_verdict_for_domain` instead, which
    prefers the JSON's `verdict` field when present."""
    if score >= 70:
        return "Clean"
    if score >= 40:
        return "Promising"
    return "Caution"


def _verdict_for_domain(domain: dict) -> str:
    """Return the verdict for one domain entry. Prefer the JSON-provided
    `verdict` field (server-computed in scripts/output.py since 2026-05-17,
    using wayback / OPR / CC backlinks gates that aren't reachable from
    the score alone). Fall back to score-only on legacy payloads."""
    server = domain.get("verdict")
    if isinstance(server, str) and server in ("Clean", "Promising", "Caution"):
        return server
    return _verdict_from_score(int(domain.get("score", 0) or 0))


def _fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def _fmt_decimal(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _verdict_style(verdict: str) -> tuple[str, str]:
    """(text_color, background_color) for the verdict pill, mirroring the
    Tailwind classes used in DomainTable.astro."""
    if verdict == "Clean":
        return "#065f46", "#ecfdf5"          # emerald-700 on emerald-50
    if verdict == "Promising":
        return "#92400e", "#fef3c7"          # amber-900 on amber-50
    return "#9f1239", "#fff1f2"              # rose-700 on rose-50


# --- Untrusted text, evidence lookups, sidecar loading -----------------------


def _is_unsafe_char(ch: str) -> bool:
    """C0/C1 control characters and the bidi embedding/override/isolate family.

    Controls break out of the line-oriented plain-text part and can confuse
    mail clients; the bidi family is worse, because it silently reorders the
    text printed AROUND it — an archived title could visually rewrite the
    domain name next to it. Same set stripped by
    scripts/archive_generator.py and src/pages/d/[domain].astro.
    """
    code = ord(ch)
    return (
        code < 0x20
        or 0x7F <= code <= 0x9F
        or 0x202A <= code <= 0x202E
        or 0x2066 <= code <= 0x2069
    )


def _clean_untrusted_text(value: Any, max_chars: int) -> str | None:
    """Normalise one piece of third-party archived text for an email body.

    Strip control/bidi characters, collapse all whitespace to single spaces
    (so a 40-line spam <title> can't become 40 lines of email), and cap the
    length INCLUDING the ellipsis. Returns None for non-strings and for
    anything empty after cleaning — callers then render nothing at all
    rather than an empty element.

    This does NOT escape for HTML; callers do that at the point of
    interpolation, because the plain-text part needs the unescaped form.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(" " if _is_unsafe_char(ch) else ch for ch in value)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[: max(1, max_chars - 1)].rstrip() + "…"
    return cleaned


def _reason_text(
    domain: dict, max_chars: int = DEFAULT_REASON_MAX_CHARS,
) -> str | None:
    """The ranker's 3-6 word justification, or None when there isn't a real one.

    None covers: field absent (mechanical-fallback days, pre-2026-09-19
    carryover), explicit null, non-string junk, whitespace-only text, and the
    "missing from response" placeholder. One None check on the render side
    covers every one of those. Mirrors scripts/output.py::_phase2_reason.
    """
    raw = domain.get("phase2_reason")
    if not isinstance(raw, str):
        return None
    if raw.strip().lower() == PHASE2_REASON_PLACEHOLDER:
        return None
    return _clean_untrusted_text(raw, max_chars)


def _excerpt_title(
    excerpts: dict[str, Any] | None,
    name: str,
    max_chars: int,
    denylist: frozenset[str] | None = None,
) -> str | None:
    """The archived page <title> for one domain, cleaned, capped and filtered.

    Four states, all of which mean "render nothing" here:
      - key absent           → we have no archived sample for this domain
      - key present but null → we looked, Wayback had nothing usable
      - title missing/blank  → capture had no usable title
      - title is boilerplate → the capture landed on a 404 / parking / server
                               default page, or the title is just the domain
                               name. "Archived page title: Page not found"
                               reads as filler and implies we checked
                               something we didn't; the honest render is no
                               line at all.
    """
    if not excerpts:
        return None
    entry = excerpts.get(name)
    if not isinstance(entry, dict):
        return None
    cleaned = _clean_untrusted_text(entry.get("title"), max_chars)
    if cleaned is None:
        return None
    deny = EXCERPT_TITLE_DENYLIST if denylist is None else denylist
    folded = cleaned.strip().strip(".").lower()
    if folded in deny:
        return None
    # A title that is just the domain (with or without its TLD) carries no
    # information the subscriber doesn't already have on the line above.
    if folded == name.lower() or folded == name.lower().rsplit(".", 1)[0]:
        return None
    return cleaned


def _resolve_path(raw: str) -> Path:
    """Config paths are repo-relative (run-daily.sh runs from the repo root,
    but a systemd unit or an operator's shell may not be)."""
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _load_sidecar_excerpts(path: Path) -> dict[str, Any]:
    """Load wayback_excerpts.json into a name → excerpt map.

    Missing, unreadable, non-JSON or non-dict → {} plus a log line. The
    newsletter is not worth failing over missing evidence; it just ships
    without archived titles that day.
    """
    if not path.exists():
        logger.info(
            "Wayback excerpt sidecar %s not present; newsletter will ship "
            "without archived titles.", path,
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Wayback excerpt sidecar %s unreadable (%s); shipping without "
            "archived titles.", path, exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Wayback excerpt sidecar %s is not a dict (got %s); shipping "
            "without archived titles.", path, type(data).__name__,
        )
        return {}
    return data


def _load_archive_names(path: Path) -> set[str]:
    """Names that have a permanent page at {site_url}/d/{name}.

    Same degrade-silently contract as the excerpt sidecar: on any problem
    return an empty set, which makes every domain fall back to the homepage
    row anchor instead of a deep link.
    """
    if not path.exists():
        logger.info(
            "Archive index %s not present; no per-domain deep links.", path,
        )
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Archive index %s unreadable (%s); no per-domain deep links.",
            path, exc,
        )
        return set()
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        logger.warning(
            "Archive index %s has no `entries` list; no per-domain deep links.",
            path,
        )
        return set()
    return {
        e["name"] for e in entries
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]
    }


def _domain_url(name: str, site_url: str, archived_names: set[str] | None) -> str:
    """Deep link to the domain's own permanent page when one exists, else the
    homepage row anchor (the pre-2026-09-19 behaviour)."""
    if (
        archived_names
        and name in archived_names
        and name
        and set(name.lower()) <= _SAFE_NAME_CHARS
    ):
        return f"{site_url}/d/{name}"
    return f"{site_url}/#{_domain_slug(name)}"


def _signals_text(domain: dict) -> str:
    """The evidence strip: one line of the four numbers that decide a pick."""
    return (
        f"Wayback {_fmt_int(domain.get('wayback_snapshots'))}"
        f" · OPR {_fmt_decimal(domain.get('open_page_rank'))}"
        f" · Backlinks {_fmt_int(domain.get('cc_source_domain_count'))}"
        f" · Score {_fmt_int(domain.get('score'))}"
    )


def _int_or_none(value: Any) -> int | None:
    """Strict: bools and floats-that-aren't-ints and strings are all None.
    The credibility line must never render a number we had to coerce."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def credibility_line(payload: dict, pick_count: int) -> str | None:
    """One sentence of provenance, built ONLY from the payload's own counts.

    CLAUDE.md hard rule 2: no invented numbers, no estimates, no rounding up.
    Every figure here is either a top-level field of daily-domains.json or
    the length of the list we are actually about to render. If ANY of the
    three payload fields is missing or isn't a non-negative int, the whole
    line is dropped — an email with no provenance line is fine, an email
    with a guessed one is not.

    `carryover_count` is deliberately unused: the picks are fresh-today only,
    so carryover doesn't belong in a sentence about what's below.
    """
    total = _int_or_none(payload.get("total_candidates_evaluated"))
    published = _int_or_none(payload.get("domain_count"))
    fresh = _int_or_none(payload.get("today_count"))
    if total is None or published is None or fresh is None:
        logger.info(
            "Credibility line omitted: payload is missing one of "
            "total_candidates_evaluated / domain_count / today_count.",
        )
        return None
    if pick_count <= 0:
        return None
    return (
        f"Evaluated {total:,} candidates today; {published:,} made the "
        f"published list, {fresh:,} of them dropped today. The "
        f"{pick_count:,} below are the highest-scoring of those fresh drops."
    )


# --- HTML body assembly ------------------------------------------------------


def _render_logos(domain: dict, site_url: str) -> str:
    """Build the affiliate-registrar logo strip for one domain. Each entry is
    an <a> wrapping an <img>, with UTM appended to the outer registrar URL.
    Unknown registrars (not in REGISTRAR_LOGO_SLUGS) are silently skipped so
    a future config entry without a hosted PNG doesn't render a broken image."""
    parts: list[str] = []
    for reg in domain.get("registrars", []) or []:
        reg_name = reg.get("name", "") if isinstance(reg, dict) else ""
        reg_url = reg.get("url", "") if isinstance(reg, dict) else ""
        logo_slug = REGISTRAR_LOGO_SLUGS.get(reg_name)
        if not logo_slug or not reg_url:
            continue
        tracked = _append_utm(reg_url)
        logo_url = LOGO_URL_TEMPLATE.format(site_url=site_url, slug=logo_slug)
        parts.append(
            f'<a href="{html.escape(tracked, quote=True)}" '
            f'style="display: inline-block; margin-right: 6px; text-decoration: none;">'
            f'<img src="{html.escape(logo_url, quote=True)}" '
            f'width="20" height="20" alt="{html.escape(reg_name)}" '
            f'style="display: inline-block; border-radius: 2px; vertical-align: middle; border: 0;">'
            f'</a>'
        )
    return "".join(parts) or "—"


def _featured_html(
    domain: dict,
    site_url: str,
    *,
    excerpts: dict[str, Any] | None,
    archived_names: set[str] | None,
    reason_max_chars: int,
    excerpt_max_chars: int,
    excerpt_denylist: frozenset[str] | None = None,
) -> str:
    """One rich block for a top pick: name + verdict, the ranker's reason,
    the archived page title, the signal strip, registrar logos.

    Every optional line is omitted entirely when its data is absent — no
    placeholder dash, no empty padded div. A pick with no reason and no
    archived title renders as name + signals + registrars, which is exactly
    what the compact rows look like with more room; it reads as a deliberate
    short entry rather than a broken one.
    """
    name = domain.get("name", "")
    verdict = _verdict_for_domain(domain)
    v_color, v_bg = _verdict_style(verdict)
    url = _domain_url(name, site_url, archived_names)
    reason = _reason_text(domain, reason_max_chars)
    archived_title = _excerpt_title(
        excerpts, name, excerpt_max_chars, excerpt_denylist,
    )

    lines: list[str] = [
        '<div style="font-size: 17px; font-weight: 600; line-height: 1.3;">'
        f'<a href="{html.escape(url, quote=True)}" '
        f'style="color: #0d6e6e; text-decoration: none;">{html.escape(name)}</a>'
        f'<span style="display: inline-block; margin-left: 8px; padding: 2px 8px; '
        f'border-radius: 9999px; background: {v_bg}; color: {v_color}; '
        f'font-size: 11px; font-weight: 500; vertical-align: middle;">{verdict}</span>'
        '</div>'
    ]
    if reason:
        lines.append(
            '<div style="margin-top: 6px; font-size: 14px; line-height: 1.5; '
            f'color: #292524;">{html.escape(reason)}</div>'
        )
    if archived_title:
        lines.append(
            '<div style="margin-top: 6px; font-size: 13px; line-height: 1.5; '
            'color: #57534e;">Archived page title: '
            f'<span style="color: #1a1a1a;">“{html.escape(archived_title)}”</span>'
            '</div>'
        )
    lines.append(
        '<div style="margin-top: 8px; font-size: 12px; color: #78716c; '
        f'line-height: 1.5;">{_signals_text(domain)}</div>'
    )
    lines.append(
        '<div style="margin-top: 10px;">'
        f'{_render_logos(domain, site_url)}</div>'
    )

    return (
        '<tr><td style="padding: 16px 0; border-bottom: 1px solid #e7e5e4;">'
        + "".join(lines)
        + '</td></tr>'
    )


def _featured_section_html(
    featured: list[dict],
    site_url: str,
    *,
    excerpts: dict[str, Any] | None,
    archived_names: set[str] | None,
    reason_max_chars: int,
    excerpt_max_chars: int,
    excerpt_denylist: frozenset[str] | None = None,
) -> str:
    """The whole featured area, or "" when there is nothing featured."""
    if not featured:
        return ""
    blocks = "\n".join(
        _featured_html(
            d, site_url,
            excerpts=excerpts,
            archived_names=archived_names,
            reason_max_chars=reason_max_chars,
            excerpt_max_chars=excerpt_max_chars,
            excerpt_denylist=excerpt_denylist,
        )
        for d in featured
    )
    return f"""    <tr>
      <td style="padding-top: 24px;">
        <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #78716c;">Today's top {len(featured)}</div>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width: 100%; border-collapse: collapse;">
{blocks}
        </table>
      </td>
    </tr>
"""


def _secondary_line(
    domain: dict,
    *,
    excerpts: dict[str, Any] | None,
    reason_max_chars: int,
    excerpt_max_chars: int,
    excerpt_denylist: frozenset[str] | None = None,
) -> str:
    """The single escaped sub-line under a compact row's domain name.

    Combines the ranker's reason with the archived page title when both are
    there, because one extra line per row is the whole budget a 17-row
    skim-list has. Returns "" when neither exists, and the caller then emits
    no element at all.
    """
    reason = _reason_text(domain, reason_max_chars)
    archived_title = _excerpt_title(
        excerpts, domain.get("name", ""), excerpt_max_chars, excerpt_denylist,
    )
    parts: list[str] = []
    if reason:
        parts.append(html.escape(reason))
    if archived_title:
        parts.append(f'was “{html.escape(archived_title)}”')
    return " — ".join(parts)


def _row_html(
    domain: dict,
    site_url: str,
    *,
    excerpts: dict[str, Any] | None = None,
    archived_names: set[str] | None = None,
    reason_max_chars: int = DEFAULT_REASON_MAX_CHARS,
    excerpt_max_chars: int = DEFAULT_COMPACT_EXCERPT_MAX_CHARS,
    excerpt_denylist: frozenset[str] | None = None,
) -> str:
    """One <tr> for a domain. All per-cell CSS inline; the responsive @media
    block in <style> targets `.ds-register-cell` to wrap the registrar logos
    onto a new full-width line below the data cells on viewports ≤600px,
    keeping the other six narrow columns side-by-side. Text content is
    HTML-escaped; href values come from trusted config-built URLs (pipeline
    already produced them) plus UTM params we control."""
    name = domain.get("name", "")
    tld = domain.get("tld", "")
    verdict = _verdict_for_domain(domain)
    v_color, v_bg = _verdict_style(verdict)

    wayback = _fmt_int(domain.get("wayback_snapshots"))
    opr = _fmt_decimal(domain.get("open_page_rank"))
    backlinks = _fmt_int(domain.get("cc_source_domain_count"))

    logos = _render_logos(domain, site_url)
    domain_anchor = _domain_url(name, site_url, archived_names)
    secondary = _secondary_line(
        domain,
        excerpts=excerpts,
        reason_max_chars=reason_max_chars,
        excerpt_max_chars=excerpt_max_chars,
        excerpt_denylist=excerpt_denylist,
    )
    secondary_html = (
        '<div style="margin-top: 3px; font-size: 11px; line-height: 1.45; '
        f'color: #78716c;">{secondary}</div>'
        if secondary else ""
    )

    return (
        '<tr style="border-bottom: 1px solid #f5f5f4;">'
        '<td style="padding: 10px 6px;">'
        f'<a href="{html.escape(domain_anchor, quote=True)}" '
        f'style="color: #0d6e6e; text-decoration: none; font-weight: 500;">'
        f'{html.escape(name)}</a>'
        f'{secondary_html}'
        '</td>'
        '<td style="padding: 10px 6px; color: #57534e; font-size: 12px; white-space: nowrap;">'
        f'.{html.escape(tld)}</td>'
        f'<td style="padding: 10px 6px; text-align: right; color: #1a1a1a; white-space: nowrap;">{wayback}</td>'
        f'<td style="padding: 10px 6px; text-align: right; color: #1a1a1a; white-space: nowrap;">{opr}</td>'
        f'<td style="padding: 10px 6px; text-align: right; color: #1a1a1a; white-space: nowrap;">{backlinks}</td>'
        '<td style="padding: 10px 6px; white-space: nowrap;">'
        f'<span style="display: inline-block; padding: 2px 8px; border-radius: 9999px; '
        f'background: {v_bg}; color: {v_color}; font-size: 11px; font-weight: 500;">'
        f'{verdict}</span>'
        '</td>'
        f'<td class="ds-register-cell" style="padding: 10px 6px; white-space: nowrap;">{logos}</td>'
        '</tr>'
    )


def _compact_table_html(rows: str, *, heading: bool) -> str:
    """The 7-column compact table, wrapped in its outer-layout <tr>.

    `heading` adds the small "The rest of today's picks" label — only
    meaningful when a featured section sits above it.
    """
    label = (
        '        <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; '
        'letter-spacing: 0.05em; color: #78716c; padding-bottom: 6px;">'
        "The rest of today's picks</div>\n"
        if heading else ""
    )
    return f"""    <tr>
      <td style="padding-top: 24px;">
{label}        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width: 100%; border-collapse: collapse; font-size: 13px;">
          <thead>
            <tr style="border-bottom: 2px solid #e7e5e4; color: #78716c; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;">
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Domain</th>
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">TLD</th>
              <th style="text-align: right; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Wayback</th>
              <th style="text-align: right; padding: 8px 6px; font-weight: 600; white-space: nowrap;">OPR</th>
              <th style="text-align: right; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Backlinks</th>
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Verdict</th>
              <th class="ds-register-cell" style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Register</th>
            </tr>
          </thead>
          <tbody>
{rows}
          </tbody>
        </table>
      </td>
    </tr>
"""


def build_html_body(
    domains: list[dict],
    today: date,
    intro_text: str,
    site_url: str = DEFAULT_SITE_URL,
    *,
    featured_n: int = 0,
    excerpts: dict[str, Any] | None = None,
    archived_names: set[str] | None = None,
    provenance: str | None = None,
    reason_max_chars: int = DEFAULT_REASON_MAX_CHARS,
    excerpt_max_chars: int = DEFAULT_EXCERPT_MAX_CHARS,
    compact_excerpt_max_chars: int = DEFAULT_COMPACT_EXCERPT_MAX_CHARS,
    excerpt_denylist: frozenset[str] | None = None,
) -> str:
    """Full HTML email body. Single 7-column table; the `<style>` block in
    `<head>` carries an @media (max-width: 600px) rule that:
      - Hides the Register `<th>` header on mobile.
      - Promotes the Register `<td>` (class `ds-register-cell`) to a full-
        width block element below the data cells, so the registrar logos
        wrap to their own line instead of fighting the other six columns
        for horizontal space.
      - Reduces horizontal padding on the other cells to 4px on mobile so
        they fit on a ~360px viewport without breaking words.

    Why a single table instead of a duplicated desktop-table + mobile-cards
    layout: a 20-domain duplicated render crossed Gmail's 102KB clip
    threshold (commit 8e479a7's email was clipped). One table renders
    compactly (~25-30KB at 20 domains, well under the limit).

    Includes `{{ unsubscribe_url }}` Buttondown template tag so subscribers
    can unsubscribe — Buttondown substitutes it server-side at send time.

    `featured_n` defaults to 0 here — the RENDERER's default is the plain
    table, and the production caller (`generate_newsletter`) passes
    config.newsletter.featured_n (3). Keeping the default at 0 means any
    caller that just wants "the table" still gets exactly that.

    `provenance` is the credibility sentence (see `credibility_line`);
    None renders no sentence at all.
    """
    featured = domains[: max(0, featured_n)]
    compact = domains[max(0, featured_n):]

    featured_html = _featured_section_html(
        featured, site_url,
        excerpts=excerpts,
        archived_names=archived_names,
        reason_max_chars=reason_max_chars,
        excerpt_max_chars=excerpt_max_chars,
        excerpt_denylist=excerpt_denylist,
    )
    rows = "\n".join(
        _row_html(
            d, site_url,
            excerpts=excerpts,
            archived_names=archived_names,
            reason_max_chars=reason_max_chars,
            excerpt_max_chars=compact_excerpt_max_chars,
            excerpt_denylist=excerpt_denylist,
        )
        for d in compact
    )
    # With every pick featured (tiny days, or featured_n >= len(domains)) the
    # table would render as a lone header row. Drop it entirely instead.
    table_html = _compact_table_html(rows, heading=bool(featured)) if compact else ""
    provenance_html = (
        '    <tr>\n'
        '      <td style="padding-top: 10px;">\n'
        '        <p style="margin: 0; font-size: 12px; line-height: 1.6; color: #78716c;">'
        f'{html.escape(provenance)}</p>\n'
        '      </td>\n'
        '    </tr>\n'
        if provenance else ""
    )
    formatted_date = today.strftime("%B %d, %Y")
    site_link = _append_utm(site_url)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DomainSifter daily picks — {formatted_date}</title>
  <style type="text/css">
    @media only screen and (max-width: 600px) {{
      th.ds-register-cell {{
        display: none !important;
      }}
      td.ds-register-cell {{
        display: block !important;
        width: 100% !important;
        padding: 6px 4px 14px 4px !important;
        border-bottom: 1px solid #e7e5e4 !important;
        white-space: normal !important;
      }}
      th:not(.ds-register-cell),
      td:not(.ds-register-cell) {{
        padding-left: 4px !important;
        padding-right: 4px !important;
      }}
    }}
  </style>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, sans-serif; color: #1a1a1a; background: #fafaf9; margin: 0; padding: 24px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="max-width: 720px; margin: 0 auto; background: #ffffff; border: 1px solid #e7e5e4; border-radius: 8px; padding: 28px;">
    <tr>
      <td>
        <h1 style="margin: 0; font-size: 22px; font-weight: 600; color: #0d6e6e; letter-spacing: -0.01em;">DomainSifter daily picks</h1>
      </td>
    </tr>
    <tr>
      <td style="padding-top: 20px;">
        <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #44403c;">{html.escape(intro_text)}</p>
      </td>
    </tr>
{provenance_html}{featured_html}{table_html}    <tr>
      <td style="padding-top: 28px; border-top: 1px solid #e7e5e4;">
        <p style="margin: 12px 0 0 0; font-size: 13px; color: #57534e;">
          See the full daily list at <a href="{html.escape(site_link, quote=True)}" style="color: #0d6e6e; text-decoration: none;">domainsifter.com</a>.
        </p>
        <p style="margin: 16px 0 0 0; font-size: 12px; color: #a8a29e; line-height: 1.5;">
          DomainSifter publishes a daily-curated list of recently-expired domains, filtered for spam, malware, and abuse signals.
        </p>
        <p style="margin: 12px 0 0 0; font-size: 11px; color: #a8a29e;">
          <a href="{{{{ unsubscribe_url }}}}" style="color: #a8a29e; text-decoration: underline;">Unsubscribe</a>
        </p>
      </td>
    </tr>
  </table>
</body>
</html>
"""


# --- Plain-text alternative part ---------------------------------------------


def _text_featured_block(
    domain: dict,
    position: int,
    site_url: str,
    *,
    excerpts: dict[str, Any] | None,
    archived_names: set[str] | None,
    reason_max_chars: int,
    excerpt_max_chars: int,
    excerpt_denylist: frozenset[str] | None = None,
) -> list[str]:
    name = domain.get("name", "")
    lines = [f"{position}. {name}  [{_verdict_for_domain(domain)}]"]
    reason = _reason_text(domain, reason_max_chars)
    if reason:
        lines.append(f"   {reason}")
    archived_title = _excerpt_title(
        excerpts, name, excerpt_max_chars, excerpt_denylist,
    )
    if archived_title:
        lines.append(f'   Archived page title: "{archived_title}"')
    lines.append(f"   {_signals_text(domain).replace(' · ', ' | ')}")
    lines.append(f"   {_domain_url(name, site_url, archived_names)}")
    return lines


def build_text_body(
    domains: list[dict],
    today: date,
    intro_text: str,
    site_url: str = DEFAULT_SITE_URL,
    *,
    featured_n: int = 0,
    excerpts: dict[str, Any] | None = None,
    archived_names: set[str] | None = None,
    provenance: str | None = None,
    reason_max_chars: int = DEFAULT_REASON_MAX_CHARS,
    excerpt_max_chars: int = DEFAULT_EXCERPT_MAX_CHARS,
    compact_excerpt_max_chars: int = DEFAULT_COMPACT_EXCERPT_MAX_CHARS,
    excerpt_denylist: frozenset[str] | None = None,
) -> str:
    """The `text/plain` alternative, built from the same data as the HTML.

    Deliberately NOT produced by stripping the HTML: an auto-stripped
    7-column table reads as a column of orphaned numbers, and a text part
    that looks like debris is worse for a spam filter than none at all.

    Two deliberate differences from the HTML part:
      - No registrar affiliate URLs. A reader seeing this part has HTML off
        or blocked; three long tracking URLs per pick would add ~60 links
        of pure noise and raise the URL-density signal that content filters
        weight heavily. The site link at the end reaches every registrar.
      - Deep links are un-tagged (no UTM query string). Plain text shows the
        URL verbatim, and a 90-character tracking tail per line is exactly
        the kind of thing that makes a text part look machine-made.

    Carries the same `{{ unsubscribe_url }}` Buttondown tag as the HTML part.
    """
    featured = domains[: max(0, featured_n)]
    compact = domains[max(0, featured_n):]

    out: list[str] = [
        f"DomainSifter daily picks — {today.strftime('%B %d, %Y')}",
        "",
        intro_text,
    ]
    if provenance:
        out += ["", provenance]

    if featured:
        featured_header = f"TODAY'S TOP {len(featured)}"
        out += ["", featured_header, "-" * len(featured_header), ""]
        for i, d in enumerate(featured, start=1):
            out += _text_featured_block(
                d, i, site_url,
                excerpts=excerpts,
                archived_names=archived_names,
                reason_max_chars=reason_max_chars,
                excerpt_max_chars=excerpt_max_chars,
                excerpt_denylist=excerpt_denylist,
            )
            out.append("")

    if compact:
        header = "THE REST OF TODAY'S PICKS" if featured else "TODAY'S PICKS"
        out += ["", header, "-" * len(header), ""]
        for i, d in enumerate(compact, start=len(featured) + 1):
            name = d.get("name", "")
            out.append(
                f"{i}. {name}  [{_verdict_for_domain(d)}]  "
                f"{_signals_text(d).replace(' · ', ' | ')}"
            )
            reason = _reason_text(d, reason_max_chars)
            archived_title = _excerpt_title(
                excerpts, name, compact_excerpt_max_chars, excerpt_denylist,
            )
            detail = " — ".join(
                p for p in (reason, f'was "{archived_title}"' if archived_title else "")
                if p
            )
            if detail:
                out.append(f"   {detail}")
            out.append(f"   {_domain_url(name, site_url, archived_names)}")
            out.append("")

    out += [
        "",
        f"Full daily list: {site_url}",
        "",
        "DomainSifter publishes a daily-curated list of recently-expired "
        "domains, filtered for spam, malware, and abuse signals.",
        "",
        "Unsubscribe: {{ unsubscribe_url }}",
        "",
    ]
    return "\n".join(out)


# --- Buttondown API ----------------------------------------------------------


class ButtondownError(RuntimeError):
    """Buttondown API call failed in a way the operator should investigate."""


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }


def _list_drafts(
    api_key: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 30,
) -> list[dict]:
    """Return every existing draft, following pagination via the `next` link."""
    session = session or requests.Session()
    drafts: list[dict] = []
    next_url: str | None = f"{BUTTONDOWN_API_BASE}/emails?status=draft"
    while next_url:
        resp = session.get(next_url, headers=_auth_headers(api_key), timeout=timeout)
        if resp.status_code != 200:
            raise ButtondownError(
                f"List drafts failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ButtondownError(f"List drafts returned non-JSON: {exc}") from exc
        drafts.extend(payload.get("results") or [])
        next_url = payload.get("next")
    return drafts


def _create_draft(
    api_key: str,
    subject: str,
    body: str,
    *,
    text_body: str | None = None,
    plaintext_field: str = "",
    session: requests.Session | None = None,
    timeout: int = 30,
) -> dict:
    """POST a new draft. status=draft means Buttondown never auto-sends it.

    Plain-text part (2026-09-19): Buttondown's documented email object has
    no field we have been able to VERIFY accepts a caller-supplied
    `text/plain` alternative — it derives one from the HTML at send time.
    So this stays opt-in: set `newsletter.plaintext_api_field` to the field
    name once it's confirmed against Buttondown's docs/support, leave it ""
    (the default) and we send exactly the payload we sent before. Inventing
    a field name here would be a guess, and a guess that 422s on the night
    of the first send costs the whole send.

    Safety valve: when the field IS configured and Buttondown rejects the
    request with a 4xx, we log and retry once WITHOUT the extra key, so a
    wrong field name degrades to the old behaviour instead of no draft.
    """
    session = session or requests.Session()
    payload: dict[str, Any] = {"subject": subject, "body": body, "status": "draft"}
    if plaintext_field and text_body:
        payload[plaintext_field] = text_body

    resp = session.post(
        f"{BUTTONDOWN_API_BASE}/emails",
        headers=_auth_headers(api_key),
        json=payload,
        timeout=timeout,
    )
    if (
        resp.status_code not in (200, 201)
        and plaintext_field in payload
        and 400 <= resp.status_code < 500
    ):
        logger.warning(
            "Buttondown rejected the draft with HTTP %s while sending the "
            "%r plain-text field; retrying without it. Check the field name "
            "against Buttondown's API docs.",
            resp.status_code, plaintext_field,
        )
        payload.pop(plaintext_field, None)
        resp = session.post(
            f"{BUTTONDOWN_API_BASE}/emails",
            headers=_auth_headers(api_key),
            json=payload,
            timeout=timeout,
        )
    if resp.status_code not in (200, 201):
        raise ButtondownError(
            f"Create draft failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ButtondownError(f"Create draft returned non-JSON: {exc}") from exc


def _already_drafted(drafts: list[dict], subject: str) -> dict | None:
    """Idempotency lookup: same subject means the script already ran today."""
    for d in drafts:
        if d.get("subject") == subject:
            return d
    return None


# --- Top-level orchestration -------------------------------------------------


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_payload(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _pick_top_n(domains: list[dict], n: int) -> list[dict]:
    """Score-desc, name-asc tie-break (matches score.score_candidates). The
    pipeline already sorts before writing, but we don't depend on that —
    sort defensively here so the newsletter is correct even if a future
    consumer mutates the JSON between pipeline write and our read."""
    return sorted(
        domains, key=lambda d: (-int(d.get("score", 0) or 0), d.get("name", "")),
    )[: max(0, n)]


def _apply_per_tld_cap(domains: list[dict], max_per_tld: int) -> list[dict]:
    """Bucket by TLD, keep top `max_per_tld` per bucket by (score desc, name asc),
    then flatten. Order returned is grouped by TLD; the caller is expected to
    score-sort afterwards (via _pick_top_n) before truncating to panel size.

    max_per_tld <= 0 or None → no-op (returns input order unchanged). The slack
    rule is implicit: TLDs with fewer than `max_per_tld` entries contribute
    everything they have; remaining panel slots get filled by other TLDs'
    entries via the downstream score-sort. No padding, no reservation.

    Mirrors applyPerTldCap() in src/components/DomainTable.astro by design —
    same input/output contract, same tie-break. The duplication is intentional
    (Python ↔ TS, no shared module) and tests guarantee the algorithm stays
    aligned. See config.display_caps._doc for the why.
    """
    if not max_per_tld or max_per_tld <= 0:
        return list(domains)

    by_tld: dict[str, list[dict]] = {}
    for d in domains:
        by_tld.setdefault(d.get("tld", ""), []).append(d)

    kept: list[dict] = []
    for entries in by_tld.values():
        entries_sorted = sorted(
            entries,
            key=lambda d: (-int(d.get("score", 0) or 0), d.get("name", "")),
        )
        kept.extend(entries_sorted[:max_per_tld])
    return kept


def _filter_to_fresh_today(domains: list[dict]) -> list[dict]:
    """Return only domains whose `days_listed == 0` — the pipeline's marker
    for "first appeared in today's run." Carryover entries (days_listed
    1-14) are visible on the site but stale from the newsletter's POV;
    sending them under a "Today's top expired domain picks" header would
    be inaccurate. See carryover.annotate_today_drops() in scripts/carryover.py.

    Treats missing days_listed as 0 — matches the frontend's same fallback
    in DomainTable.astro (legacy payloads / sample data). Defensible because
    the only producer that omits the field is sample-domains.json, which
    represents a notional "all fresh" preview state.
    """
    return [d for d in domains if (d.get("days_listed") or 0) == 0]


def generate_newsletter(
    config: dict,
    payload: dict,
    *,
    api_key: str | None = None,
    today: date | None = None,
    session: requests.Session | None = None,
    dry_run: bool = False,
) -> dict:
    """Build the body, check for an existing draft, create one if needed.

    Returns a status dict (always — never raises on disabled/empty/duplicate).
    Buttondown API errors DO raise ButtondownError; the CLI catches them and
    exits non-zero, the run-daily wrapper logs and continues.
    """
    nl_cfg = config.get("newsletter", {}) or {}
    if not nl_cfg.get("enabled", False):
        logger.info("Newsletter feature disabled (config.newsletter.enabled=false).")
        return {"status": "disabled"}

    top_n = int(nl_cfg.get("top_n", DEFAULT_TOP_N))
    featured_n = int(nl_cfg.get("featured_n", DEFAULT_FEATURED_N) or 0)
    subject_template = nl_cfg.get("subject_template", DEFAULT_SUBJECT_TEMPLATE)
    intro_text = nl_cfg.get("intro_text", DEFAULT_INTRO)
    site_url = nl_cfg.get("site_url", DEFAULT_SITE_URL).rstrip("/")
    reason_max_chars = int(nl_cfg.get("reason_max_chars", DEFAULT_REASON_MAX_CHARS))
    excerpt_max_chars = int(nl_cfg.get("excerpt_max_chars", DEFAULT_EXCERPT_MAX_CHARS))
    compact_excerpt_max_chars = int(
        nl_cfg.get("compact_excerpt_max_chars", DEFAULT_COMPACT_EXCERPT_MAX_CHARS)
    )
    plaintext_field = str(nl_cfg.get("plaintext_api_field", "") or "")
    raw_denylist = nl_cfg.get("excerpt_title_denylist")
    excerpt_denylist = (
        frozenset(str(t).strip().lower() for t in raw_denylist)
        if isinstance(raw_denylist, list) else EXCERPT_TITLE_DENYLIST
    )

    today = today or date.today()
    domains = payload.get("domains", []) or []
    if not domains:
        logger.warning(
            "daily-domains.json contains zero domains; skipping newsletter."
        )
        return {"status": "skipped_empty"}

    fresh_today = _filter_to_fresh_today(domains)
    if not fresh_today:
        # Distinct status from skipped_empty: there ARE domains in the JSON
        # (carryover), they're just stale-from-fresh-today's perspective.
        # Sending a draft of carryover would mislabel days-old finds as
        # "today's drops." Owner's policy: serve only what's fresh, even if
        # that means no draft on a low-drop day.
        logger.info(
            "No fresh domains today (all %d entries are carryover); "
            "skipping draft creation.",
            len(domains),
        )
        return {"status": "skipped_no_fresh"}

    # Per-TLD diversity cap (added 2026-05-25). Applied BEFORE top-N truncation
    # so a single TLD can't crowd out the score-sort. With panel=20 and cap=8,
    # at most 8 entries from any one TLD appear; the remaining 12 slots fill
    # by score across other TLDs. Slack is implicit — TLDs with fewer than the
    # cap just contribute what they have. max_per_tld <= 0 disables the cap.
    display_caps = config.get("display_caps", {}) or {}
    max_per_tld = int(display_caps.get("max_per_tld_in_top_panel", 0) or 0)
    capped = _apply_per_tld_cap(fresh_today, max_per_tld)

    top_domains = _pick_top_n(capped, top_n)
    if not top_domains:
        logger.warning("top_n=%d yielded zero domains; skipping newsletter.", top_n)
        return {"status": "skipped_empty"}

    # Evidence sidecars. Both degrade silently to "no extra content" — a
    # missing archive index just means no deep links, a missing excerpt
    # sidecar just means no archived titles. Neither is worth losing a send.
    excerpts = _load_sidecar_excerpts(
        _resolve_path(
            nl_cfg.get("sidecar_excerpts_path", DEFAULT_SIDECAR_EXCERPTS_PATH)
        )
    )
    archived_names = _load_archive_names(
        _resolve_path(nl_cfg.get("archive_index_path", DEFAULT_ARCHIVE_INDEX_PATH))
    )
    provenance = credibility_line(payload, len(top_domains))

    formatted_date = today.strftime("%B %d, %Y")
    subject = subject_template.format(n=len(top_domains), date=formatted_date)
    render_kwargs: dict[str, Any] = {
        "site_url": site_url,
        "featured_n": featured_n,
        "excerpts": excerpts,
        "archived_names": archived_names,
        "provenance": provenance,
        "reason_max_chars": reason_max_chars,
        "excerpt_max_chars": excerpt_max_chars,
        "compact_excerpt_max_chars": compact_excerpt_max_chars,
        "excerpt_denylist": excerpt_denylist,
    }
    body = build_html_body(top_domains, today, intro_text, **render_kwargs)
    text_body = build_text_body(top_domains, today, intro_text, **render_kwargs)

    if dry_run:
        logger.info(
            "Dry run: subject=%r body_chars=%d text_chars=%d domains=%d "
            "featured=%d (would not POST).",
            subject, len(body), len(text_body), len(top_domains), featured_n,
        )
        return {
            "status": "dry_run",
            "subject": subject,
            "body": body,
            "text_body": text_body,
            "body_chars": len(body),
            "text_chars": len(text_body),
            "domain_count": len(top_domains),
        }

    if not api_key:
        raise RuntimeError(
            "BUTTONDOWN_API_KEY missing. Set it in the .env file on the OVH "
            "server (or disable the newsletter in config.json by setting "
            "newsletter.enabled=false)."
        )

    session = session or requests.Session()
    existing_drafts = _list_drafts(api_key, session=session)
    existing = _already_drafted(existing_drafts, subject)
    if existing:
        logger.info(
            "Draft already exists for subject %r (id=%s); not creating duplicate.",
            subject, existing.get("id"),
        )
        return {
            "status": "skipped_duplicate",
            "id": existing.get("id"),
            "subject": subject,
        }

    created = _create_draft(
        api_key, subject, body,
        text_body=text_body,
        plaintext_field=plaintext_field,
        session=session,
    )
    logger.info(
        "Newsletter draft created on Buttondown: id=%s subject=%r body_chars=%d "
        "text_chars=%d domains=%d featured=%d",
        created.get("id"), subject, len(body), len(text_body),
        len(top_domains), featured_n,
    )
    return {
        "status": "created",
        "id": created.get("id"),
        "subject": subject,
        "domain_count": len(top_domains),
        "text_chars": len(text_body),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.generate_newsletter",
        description="Generate a Buttondown daily-newsletter draft from "
                    "today's daily-domains.json.",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to scripts/config.json (default: scripts/config.json next to this module)",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Path to daily-domains.json (default: config.output_path)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the HTML body and print it; do NOT POST to Buttondown.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="With --dry-run, print the text/plain alternative part instead "
             "of the HTML body (so each can be piped to a file cleanly).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        return 1

    config = load_config(config_path)
    input_path = Path(
        args.input or config.get("output_path", "src/data/daily-domains.json")
    )
    if not input_path.exists():
        logger.error(
            "daily-domains.json not found at %s — run the pipeline first.",
            input_path,
        )
        return 1

    payload = load_payload(input_path)
    api_key = (os.environ.get("BUTTONDOWN_API_KEY") or "").strip() or None

    try:
        result = generate_newsletter(
            config, payload, api_key=api_key, dry_run=args.dry_run,
        )
    except ButtondownError as exc:
        logger.error("Buttondown API error: %s", exc)
        return 2
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # pragma: no cover — defence-in-depth
        logger.exception("Newsletter generation crashed: %s", exc)
        return 1

    if result.get("status") == "dry_run":
        print(result["text_body"] if args.text else result["body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
