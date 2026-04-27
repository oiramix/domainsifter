"""Write the daily JSON contract consumed by the Astro frontend.

The site reads `src/data/daily-domains.json`. The shape is locked in
PLAN.md Principle 5 with one schema migration applied 2026-04-27 evening:
the single `affiliate_link` string was replaced by a `registrars` array.

Output shape:
    {
        "generated_at": "2026-04-27T06:00:00Z",
        "domain_count": 487,
        "domains": [
            {
                "name": "example.com",
                "tld": "com",
                "dropped_date": "2026-04-26",
                "wayback_snapshots": 142,
                "wayback_last_snapshot": "2024-08-15",
                "open_page_rank": 3.7,
                "cert_history": true,
                "previous_registrar": "GoDaddy",
                "score": 78,
                "registrars": [
                    {"name": "Namecheap", "url": "https://namecheap.pxf.io/..."},
                    {"name": "NameSilo",  "url": "https://www.namesilo.com/..."}
                ]
            }
        ]
    }

The registrars list comes from config.registrars and preserves order.
The {name} placeholder in each `link_template` is substituted with the
apex domain via plain str.replace — NOT str.format. The current Namecheap
URL contains literal `%3D` (`=`) and similar percent-encoded characters
that `.format()` would either error on or mishandle as positional refs.

`write_output(...)` takes already-scored, already-sorted candidates and:
- caps at config.max_candidates_for_publication (CEILING, not a quota — if
  fewer survived, we publish all of them; we never pad the list to hit the
  cap with weak candidates).
- builds each candidate's registrars list from config.registrars
- projects each candidate to ONLY the contract fields (no internal
  enrichment metadata leaks into the public JSON)
- writes atomically (temp file + os.replace) so partial writes never
  serve a half-baked file to Cloudflare Pages
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRACT_FIELDS = (
    "name",
    "tld",
    "dropped_date",
    "wayback_snapshots",
    "wayback_last_snapshot",
    "open_page_rank",
    "cert_history",
    "previous_registrar",
    "score",
    "registrars",
)


def _build_registrars(name: str, configured: list[dict]) -> list[dict]:
    """Substitute {name} in every configured registrar's link_template.

    Order is preserved from config — that's the order the popover renders.
    Plain str.replace, NOT str.format: the templates contain literal
    percent-encoded characters that confuse .format().
    """
    out: list[dict] = []
    for entry in configured:
        if not isinstance(entry, dict):
            continue
        reg_name = entry.get("name")
        template = entry.get("link_template")
        if not reg_name or not template:
            continue
        out.append({"name": reg_name, "url": template.replace("{name}", name)})
    return out


def _project(candidate: dict, registrars_config: list[dict]) -> dict:
    name = candidate.get("name", "")
    return {
        "name": name,
        "tld": candidate.get("tld", name.rsplit(".", 1)[-1] if "." in name else ""),
        "dropped_date": candidate.get("dropped_date"),
        "wayback_snapshots": candidate.get("wayback_snapshots"),
        "wayback_last_snapshot": candidate.get("wayback_last_snapshot"),
        "open_page_rank": candidate.get("open_page_rank"),
        "cert_history": candidate.get("cert_history"),
        "previous_registrar": candidate.get("previous_registrar"),
        "score": candidate.get("score", 0),
        "registrars": _build_registrars(name, registrars_config),
    }


def build_payload(
    candidates: list[dict],
    config: dict,
    *,
    generated_at: datetime | None = None,
) -> dict:
    """Build the final JSON payload (does not write to disk).

    Cap precedence: max_candidates_for_publication wins; max_candidates_per_day
    kept as a fallback so older configs / tests still parse. The cap is a
    CEILING — if `candidates` has fewer entries than the cap, all are emitted.
    """
    cap = int(
        config.get("max_candidates_for_publication")
        or config.get("max_candidates_per_day", 500)
    )
    registrars_config = config.get("registrars") or []
    capped = candidates[:cap]
    domains = [_project(c, registrars_config) for c in capped]
    when = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated_at": when,
        "domain_count": len(domains),
        "domains": domains,
    }


def write_output(
    candidates: list[dict],
    config: dict,
    output_path: str | Path | None = None,
    *,
    generated_at: datetime | None = None,
) -> Path:
    """Build the payload and write it atomically. Returns the written path."""
    target = Path(output_path or config.get("output_path", "src/data/daily-domains.json"))
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = build_payload(candidates, config, generated_at=generated_at)

    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", dir=str(target.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    logger.info(
        "Wrote %d domains to %s (generated_at=%s)",
        payload["domain_count"],
        target,
        payload["generated_at"],
    )
    return target
