"""Daily newsletter draft generator — Buttondown API integration.

Reads `src/data/daily-domains.json`, takes the top N domains by score, builds
an HTML email body, and POSTs it to Buttondown as a draft. The draft is
never auto-sent — Mario uses Buttondown's dashboard "Send draft" to QA
against his own email, then flips status to `about_to_send` when satisfied.

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

Exit codes:
    0 — draft created OR skipped (duplicate / disabled / empty)
    1 — config or input file missing / unreadable
    2 — Buttondown API failure

Configuration sources:
    config["newsletter"]["enabled"]           — feature flag (default False)
    config["newsletter"]["top_n"]             — default 20
    config["newsletter"]["subject_template"]  — uses {n} and {date} placeholders
    config["newsletter"]["intro_text"]        — body intro paragraph
    config["newsletter"]["site_url"]          — for "see full list" link
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

DEFAULT_TOP_N = 20
DEFAULT_SUBJECT_TEMPLATE = "DomainSifter daily — {n} picks for {date}"
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
    """Same thresholds as DomainTable.astro's verdictFromScore — keeps the
    email and the live site visually consistent."""
    if score >= 70:
        return "Clean"
    if score >= 40:
        return "Promising"
    return "Caution"


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


# --- HTML body assembly ------------------------------------------------------


def _row_html(domain: dict, site_url: str) -> str:
    """One <tr> for a domain. All CSS inline (email clients strip <style>).
    Text content is HTML-escaped; href values come from trusted config-built
    URLs (pipeline already produced them) plus UTM params we control."""
    name = domain.get("name", "")
    tld = domain.get("tld", "")
    score = int(domain.get("score", 0) or 0)
    verdict = _verdict_from_score(score)
    v_color, v_bg = _verdict_style(verdict)
    slug = _domain_slug(name)

    wayback = _fmt_int(domain.get("wayback_snapshots"))
    opr = _fmt_decimal(domain.get("open_page_rank"))
    backlinks = _fmt_int(domain.get("cc_source_domain_count"))

    logos_parts: list[str] = []
    for reg in domain.get("registrars", []) or []:
        reg_name = reg.get("name", "") if isinstance(reg, dict) else ""
        reg_url = reg.get("url", "") if isinstance(reg, dict) else ""
        logo_slug = REGISTRAR_LOGO_SLUGS.get(reg_name)
        if not logo_slug or not reg_url:
            continue
        tracked = _append_utm(reg_url)
        logo_url = LOGO_URL_TEMPLATE.format(site_url=site_url, slug=logo_slug)
        logos_parts.append(
            f'<a href="{html.escape(tracked, quote=True)}" '
            f'style="display: inline-block; margin-right: 6px; text-decoration: none;">'
            f'<img src="{html.escape(logo_url, quote=True)}" '
            f'width="20" height="20" alt="{html.escape(reg_name)}" '
            f'style="display: inline-block; border-radius: 2px; vertical-align: middle; border: 0;">'
            f'</a>'
        )
    logos = "".join(logos_parts) or "—"

    domain_anchor = f"{site_url}/#{slug}"

    return (
        '<tr style="border-bottom: 1px solid #f5f5f4;">'
        '<td style="padding: 10px 6px;">'
        f'<a href="{html.escape(domain_anchor, quote=True)}" '
        f'style="color: #0d6e6e; text-decoration: none; font-weight: 500;">'
        f'{html.escape(name)}</a>'
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
        f'<td style="padding: 10px 6px; white-space: nowrap;">{logos}</td>'
        '</tr>'
    )


def build_html_body(
    domains: list[dict],
    today: date,
    intro_text: str,
    site_url: str = DEFAULT_SITE_URL,
) -> str:
    """Full HTML email body, inline-styled, 720px max-width nested-table layout.

    Includes `{{ unsubscribe_url }}` Buttondown template tag so subscribers
    can unsubscribe — Buttondown substitutes it server-side at send time.
    """
    rows = "\n".join(_row_html(d, site_url) for d in domains)
    formatted_date = today.strftime("%B %d, %Y")
    site_link = _append_utm(site_url)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DomainSifter daily — {formatted_date}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Inter, sans-serif; color: #1a1a1a; background: #fafaf9; margin: 0; padding: 24px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="max-width: 720px; margin: 0 auto; background: #ffffff; border: 1px solid #e7e5e4; border-radius: 8px; padding: 28px;">
    <tr>
      <td>
        <h1 style="margin: 0; font-size: 22px; font-weight: 600; color: #0d6e6e; letter-spacing: -0.01em;">DomainSifter</h1>
        <p style="margin: 4px 0 0 0; font-size: 13px; color: #78716c;">Daily picks · {formatted_date}</p>
      </td>
    </tr>
    <tr>
      <td style="padding-top: 20px;">
        <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #44403c;">{html.escape(intro_text)}</p>
      </td>
    </tr>
    <tr>
      <td style="padding-top: 24px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width: 100%; border-collapse: collapse; font-size: 13px;">
          <thead>
            <tr style="border-bottom: 2px solid #e7e5e4; color: #78716c; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;">
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Domain</th>
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">TLD</th>
              <th style="text-align: right; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Wayback</th>
              <th style="text-align: right; padding: 8px 6px; font-weight: 600; white-space: nowrap;">OPR</th>
              <th style="text-align: right; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Backlinks</th>
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Verdict</th>
              <th style="text-align: left; padding: 8px 6px; font-weight: 600; white-space: nowrap;">Register</th>
            </tr>
          </thead>
          <tbody>
{rows}
          </tbody>
        </table>
      </td>
    </tr>
    <tr>
      <td style="padding-top: 28px; border-top: 1px solid #e7e5e4;">
        <p style="margin: 12px 0 0 0; font-size: 13px; color: #57534e;">
          See the full daily list at <a href="{html.escape(site_link, quote=True)}" style="color: #0d6e6e; text-decoration: none;">domainsifter.com</a>.
        </p>
        <p style="margin: 16px 0 0 0; font-size: 12px; color: #a8a29e; line-height: 1.5;">
          DomainSifter publishes a daily-curated list of recently-expired domains, filtered for spam, malware, and abuse signals. Estonia-based independent project.
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
    session: requests.Session | None = None,
    timeout: int = 30,
) -> dict:
    """POST a new draft. status=draft means Buttondown never auto-sends it."""
    session = session or requests.Session()
    resp = session.post(
        f"{BUTTONDOWN_API_BASE}/emails",
        headers=_auth_headers(api_key),
        json={"subject": subject, "body": body, "status": "draft"},
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
    subject_template = nl_cfg.get("subject_template", DEFAULT_SUBJECT_TEMPLATE)
    intro_text = nl_cfg.get("intro_text", DEFAULT_INTRO)
    site_url = nl_cfg.get("site_url", DEFAULT_SITE_URL).rstrip("/")

    today = today or date.today()
    domains = payload.get("domains", []) or []
    if not domains:
        logger.warning(
            "daily-domains.json contains zero domains; skipping newsletter."
        )
        return {"status": "skipped_empty"}

    top_domains = _pick_top_n(domains, top_n)
    if not top_domains:
        logger.warning("top_n=%d yielded zero domains; skipping newsletter.", top_n)
        return {"status": "skipped_empty"}

    formatted_date = today.strftime("%B %d, %Y")
    subject = subject_template.format(n=len(top_domains), date=formatted_date)
    body = build_html_body(top_domains, today, intro_text, site_url=site_url)

    if dry_run:
        logger.info(
            "Dry run: subject=%r body_chars=%d domains=%d (would not POST).",
            subject, len(body), len(top_domains),
        )
        return {
            "status": "dry_run",
            "subject": subject,
            "body": body,
            "body_chars": len(body),
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

    created = _create_draft(api_key, subject, body, session=session)
    logger.info(
        "Newsletter draft created on Buttondown: id=%s subject=%r body_chars=%d domains=%d",
        created.get("id"), subject, len(body), len(top_domains),
    )
    return {
        "status": "created",
        "id": created.get("id"),
        "subject": subject,
        "domain_count": len(top_domains),
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
        print(result["body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
