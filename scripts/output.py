"""Write the daily JSON contract consumed by the Astro frontend.

The site reads `src/data/daily-domains.json`. The shape is locked in
PLAN.md Principle 5 — pipeline produces it, site consumes it. If you
need to change the schema, update both sides in lockstep.

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
                "affiliate_link": "https://..."
            }
        ]
    }

`write_output(...)` takes already-scored, already-sorted candidates and:
- caps at config.max_candidates_per_day
- builds an affiliate_link from config.affiliate_link_template
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
    "affiliate_link",
)


def _project(candidate: dict, template: str) -> dict:
    name = candidate.get("name", "")
    affiliate = template.format(name=name) if template else ""
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
        "affiliate_link": affiliate,
    }


def build_payload(
    candidates: list[dict],
    config: dict,
    *,
    generated_at: datetime | None = None,
) -> dict:
    """Build the final JSON payload (does not write to disk)."""
    cap = int(config.get("max_candidates_per_day", 500))
    template = config.get("affiliate_link_template", "")
    capped = candidates[:cap]
    domains = [_project(c, template) for c in capped]
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
