"""One-shot: re-apply structural filter and recompute verdicts on today's
daily-domains.json. No network calls; touches only the JSON.

Use after a filter/verdict logic change to clean already-published data
without triggering a full pipeline re-run.

Usage (from repo root, venv active):
    python -m scripts.refilter_today
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.filter import keep_structural
from scripts.output import _compute_verdict

CONFIG_PATH = Path("scripts/config.json")
JSON_PATH = Path("src/data/daily-domains.json")


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text())
    payload = json.loads(JSON_PATH.read_text())

    original = payload["domains"]
    kept: list[dict] = []
    rejected: list[tuple[str, str]] = []
    verdict_counts: dict[str, int] = {}

    for domain in original:
        ok, reason = keep_structural(domain, config)
        if not ok:
            rejected.append((domain["name"], reason or "rejected"))
            continue
        verdict = _compute_verdict(domain, config)
        domain["verdict"] = verdict
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        kept.append(domain)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_count = sum(1 for d in kept if d.get("dropped_date") == today)

    payload["domains"] = kept
    payload["domain_count"] = len(kept)
    payload["today_count"] = today_count
    payload["carryover_count"] = len(kept) - today_count
    payload["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    JSON_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    print(f"Re-filter complete: {len(original)} -> {len(kept)} kept, {len(rejected)} rejected")
    print(f"Verdict distribution: {verdict_counts}")
    if rejected:
        print("\nRejected domains:")
        for name, reason in rejected:
            print(f"  - {name}: {reason}")


if __name__ == "__main__":
    main()
