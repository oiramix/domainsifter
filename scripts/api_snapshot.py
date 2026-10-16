"""Build the read-only API snapshot from DomainSifter's private stores.

Runs on a timer as the `domainsifter` user (it needs R2 credentials). It
reads:

  - the private domain archive in R2, state/domain_archive/YYYY-MM.jsonl:
    one event per day a domain was confirmed available (`live`, from
    2026-06-03) or appeared on the published list (`backfill`, 2026-04-28 to
    2026-06-02); see scripts/domain_archive.py;
  - the Wayback excerpt sidecar (src/data/wayback_excerpts.json) for each
    domain's most recent archived page title;

and writes ONE JSON file (`api.snapshot_path`) holding every domain already
shaped for the API: provenance on every value, null + status where a value
is unknown, and the full observation history. scripts/api_server.py serves
that file and nothing else, so the internet-facing process needs no R2
credentials and no access to the repo's secrets.

Unknown is null, never 0. Where the pipeline did not record WHY a value is
missing (Common Crawl count, Open PageRank), the status is `not_checked`;
docs/api/data-dictionary.md spells that ambiguity out.

The toxic denylist and the held queue are NOT read: neither leaves
DomainSifter.

    python -m scripts.api_snapshot            # build and write
    python -m scripts.api_snapshot --dry-run  # build, print counts, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ARCHIVE_PREFIX = "state/domain_archive/"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "scripts" / "config.json"

# The classifier's real verdicts; "unknown" is its failure value.
_CATEGORIES = ("legitimate", "parked", "empty", "toxic")
_SPAM_SIGNAL = {"toxic": "high", "legitimate": "low", "parked": "low", "empty": "low"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _ts(day: str | None) -> str | None:
    """'YYYY-MM-DD' -> 'YYYY-MM-DDT00:00:00Z'. The archive records days, not
    times, so every timestamp derived from it has day precision."""
    try:
        return date.fromisoformat(str(day)).isoformat() + "T00:00:00Z"
    except (TypeError, ValueError):
        return None


def _year(day: Any) -> int | None:
    try:
        return date.fromisoformat(str(day)[:10]).year
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_archive(s3: Any, bucket: str) -> list[dict]:
    """Every event in the R2 domain archive. A corrupt line is skipped; an R2
    error propagates (the run fails and the previous snapshot stays live)."""
    from scripts import cc_refresh, domain_archive

    events: list[dict] = []
    for key in cc_refresh._list_keys(s3, bucket, ARCHIVE_PREFIX):
        if key.endswith(".jsonl"):
            events.extend(domain_archive._parse_jsonl(
                domain_archive._r2_get_object_or_empty(s3, bucket, key)
            ))
    return events


def load_excerpts(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Excerpt sidecar %s unavailable (%s); sample_title will be null", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ---------------------------------------------------------------------------
# Shaping one domain
# ---------------------------------------------------------------------------


def _events_by_day(events: list[dict]) -> list[dict]:
    """One event per day, oldest first; a live record beats a backfill one."""
    by_day: dict[str, dict] = {}
    for ev in events:
        day = ev.get("availability_confirmed_date")
        if _ts(day) is None:
            continue
        if day not in by_day or (ev.get("source") == "live" and by_day[day].get("source") != "live"):
            by_day[day] = ev
    return [by_day[d] for d in sorted(by_day)]


def _availability_obs(ev: dict, observed_at: str, rdap_since: str) -> dict:
    rdap = ev.get("source") == "live" or ev["availability_confirmed_date"] >= rdap_since
    return {
        "observed_at": observed_at,
        "kind": "availability",
        "name": "availability",
        "value": "available" if rdap else "dropped",
        "status": "ok",
        "source": "registry_rdap" if rdap else "icann_czds",
    }


def _metric_obs(name: str, value: Any, source: str, observed_at: str) -> dict:
    return {
        "observed_at": observed_at,
        "kind": "metric",
        "name": name,
        "value": value,
        "status": "ok" if value is not None else "not_checked",
        "source": source,
    }


def _wayback_status(ev: dict) -> str:
    snapshots = _num(ev.get("wayback_snapshots"))
    if snapshots is not None:
        return "ok" if snapshots > 0 else "none_found"
    if ev.get("wayback_unknown") is True:
        return "lookup_failed"
    return "not_checked"


def _observations(ev: dict, rdap_since: str) -> list[dict]:
    at = _ts(ev["availability_confirmed_date"])
    cert = ev.get("cert_history")
    obs = [
        _availability_obs(ev, at, rdap_since),
        _metric_obs("referring_domains", _num(ev.get("cc_source_domain_count")),
                    "common_crawl_webgraph", at),
        _metric_obs("open_page_rank", _num(ev.get("open_page_rank")), "open_page_rank", at),
        _metric_obs("cert_history", cert if isinstance(cert, bool) else None, "crtsh", at),
        {
            "observed_at": at,
            "kind": "wayback",
            "name": "snapshots",
            "value": _num(ev.get("wayback_snapshots")),
            "status": _wayback_status(ev),
            "source": "internet_archive_wayback",
        },
    ]
    category = ev.get("snapshot_category")
    obs.append({
        "observed_at": at,
        "kind": "analysis",
        "name": "content_category",
        "value": category if category in _CATEGORIES else None,
        "status": "ok" if category in _CATEGORIES else "not_checked",
        "source": "domainsifter_content_classifier",
    })
    score = _num(ev.get("phase2_score"))
    obs.append({
        "observed_at": at,
        "kind": "analysis",
        "name": "name_quality",
        "value": score,
        "status": "ok" if score is not None else "not_checked",
        "source": "domainsifter_name_ranker",
    })
    return obs


def _latest(observations: list[dict], kind: str, name: str) -> dict | None:
    """The newest observation with status ok, else the newest of any status."""
    matching = [o for o in observations if o["kind"] == kind and o["name"] == name]
    ok = [o for o in matching if o["status"] == "ok"]
    pool = ok or matching
    return max(pool, key=lambda o: o["observed_at"]) if pool else None


def _metric(observations: list[dict], name: str) -> dict:
    o = _latest(observations, "metric", name)
    return {
        "name": name,
        "value": o["value"] if o else None,
        "status": o["status"] if o else "not_checked",
        "source": o["source"] if o else None,
        "observed_at": o["observed_at"] if o else None,
    }


def build_domain(domain: str, events: list[dict], excerpt: Any, config: dict) -> dict | None:
    """The API object for one domain, or None if it has no dated event."""
    api_cfg = config.get("api", {}) or {}
    rdap_since = str(api_cfg.get("rdap_verified_since", "2026-04-30"))
    days = _events_by_day(events)
    if not days:
        return None

    observations = [o for ev in days for o in _observations(ev, rdap_since)]
    first, last = days[0], days[-1]
    avail = _latest(observations, "availability", "availability")

    # Wayback: newest event that actually answered, else the newest event.
    wb_event = next((ev for ev in reversed(days) if _wayback_status(ev) in ("ok", "none_found")), last)
    wb_status = _wayback_status(wb_event)
    title = excerpt.get("title") if isinstance(excerpt, dict) else None

    category_obs = _latest(observations, "analysis", "content_category")
    category = category_obs["value"] if category_obs and category_obs["status"] == "ok" else None
    category_event = next(
        (ev for ev in reversed(days) if ev.get("snapshot_category") == category), None,
    ) if category else None
    name_obs = _latest(observations, "analysis", "name_quality")
    name_score = name_obs["value"] if name_obs and name_obs["status"] == "ok" else None

    analysis = None
    if category or name_score is not None:
        analysis = {
            "model": str(api_cfg.get("classifier_model", "claude-haiku-4-5")) if category else None,
            "prompt_version": category_event.get("snapshot_classifier_version") if category_event else None,
            "niche": None,
            "score": None,
            "spam_signal": _SPAM_SIGNAL.get(category) if category else None,
            "verdict": category,
            "analyzed_at": category_obs["observed_at"] if category else None,
            "name_quality": None if name_score is None else {
                "model": str(api_cfg.get("name_ranker_model", "claude-haiku-4-5")),
                "score": name_score,
                "analyzed_at": name_obs["observed_at"],
            },
        }

    return {
        "domain": domain,
        "tld": domain.rsplit(".", 1)[-1],
        "first_seen_at": _ts(first["availability_confirmed_date"]),
        "last_seen_at": _ts(last["availability_confirmed_date"]),
        "availability": {
            "status": avail["value"] if avail else "unknown",
            "observed_at": avail["observed_at"] if avail else None,
            "source": avail["source"] if avail else None,
            "drop_date": next((ev.get("drop_date") for ev in reversed(days) if ev.get("drop_date")), None),
        },
        "metrics": [_metric(observations, n) for n in ("referring_domains", "open_page_rank", "cert_history")],
        "wayback": {
            "status": wb_status,
            "snapshots": _num(wb_event.get("wayback_snapshots")),
            "first_year": None,
            "last_year": _year(wb_event.get("wayback_last_snapshot")),
            "sample_title": title if isinstance(title, str) and title.strip() else None,
            "observed_at": _ts(wb_event["availability_confirmed_date"]),
        },
        "analysis": analysis,
        "published_on_domainsifter": any(ev.get("was_published") is True for ev in days),
        "observations_count": len(observations),
        "observations": observations,
    }


def build_snapshot(
    events: list[dict],
    excerpts: dict[str, Any],
    config: dict,
    *,
    now: datetime | None = None,
    git_sha: str | None = None,
) -> dict:
    by_domain: dict[str, list[dict]] = {}
    for ev in events:
        name = str(ev.get("domain") or "").strip().lower().rstrip(".")
        if name and "." in name:
            by_domain.setdefault(name, []).append(ev)
    domains = {}
    for name in sorted(by_domain):
        doc = build_domain(name, by_domain[name], excerpts.get(name), config)
        if doc:
            domains[name] = doc
    built_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at,
        "git_sha": git_sha,
        "domains": domains,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_snapshot(snapshot: dict, path: Path) -> None:
    """Atomic write, world-readable: the API runs as a different user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(
    argv: list[str] | None = None,
    *,
    s3_factory: Callable[[], tuple[Any, str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Build the read-only API snapshot")
    parser.add_argument("--config", default=str(_CONFIG_PATH))
    parser.add_argument("--output", help="override api.snapshot_path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        api_cfg = config.get("api", {}) or {}
        if s3_factory is None:
            from scripts import diff

            def s3_factory() -> tuple[Any, str]:
                return diff._r2_client(), diff._bucket()

        s3, bucket = s3_factory()
        events = load_archive(s3, bucket)
        sidecar = Path(config.get("sidecar_excerpts_path", "src/data/wayback_excerpts.json"))
        excerpts = load_excerpts(sidecar if sidecar.is_absolute() else _REPO_ROOT / sidecar)
        snapshot = build_snapshot(events, excerpts, config, git_sha=_git_sha())
        logger.info(
            "API snapshot: %d archive events -> %d domains (built_at %s)",
            len(events), len(snapshot["domains"]), snapshot["built_at"],
        )
        if args.dry_run:
            return 0
        out = Path(args.output or api_cfg.get("snapshot_path", "/var/lib/domainsifter-api/snapshot.json"))
        write_snapshot(snapshot, out)
        logger.info("API snapshot written to %s", out)
        return 0
    except Exception:
        logger.exception("API snapshot build failed; the previous snapshot stays in place")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
