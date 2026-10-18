"""Rebuild shortlist, step 1: which domains link to promising available domains.

WHY
---
`cc_source_domain_count` says HOW MANY distinct domains link to a candidate
in the Common Crawl domain graph, not WHICH ones. Judging whether those
links are editorial and on-topic (step 2, not built) needs the referring
domain names. This tool produces them.

WHY THE FULL DROP LIST (2026-10-15)
-----------------------------------
The first version read the available-set handoff, but every domain there had
already passed phase2_ranker (name brandability) and the per-host RDAP caps.
Old local-business and org sites, the ones with real backlinks and plain or
long keyword names, never got that far, so the counts were tiny. The input
is now every drop of the day (scripts/state/drops_latest.jsonl, written by
pipeline._emit_drops before any filter), with only abuse filters applied.
Content is not judged here; step 2 does that.

STANDALONE, BY HAND
-------------------
Not wired into pipeline.py, send_report.py or cc_refresh.py. Run it once by
hand after the daily run (it refuses inside the cc_refresh blackout window
or while the pipeline unit is active, unless --force):

    python -m scripts.rebuild_referrers

WHAT IT DOES
------------
1. Picks the Common Crawl release: the configured `cc_backlinks.latest_release`
   if its raw vertices + edges are still in R2 under cc/raw/{release}/
   (cc_refresh prunes raw to the newest `prune_raw_after_releases`), else the
   newest retained release.
2. Counts every drop's linking domains from that SAME release's derived
   SQLite (cc_apex), so counts and referrer lists come from one graph. Logs
   the distribution over `rebuild.distribution_buckets`.
3. Abuse filters only, on every drop in the graph: the toxic denylist (R2),
   the rejected-keyword lists (same matchers as filter.py), the rebuild-only
   gambling terms in `rebuild.extra_rejected_terms` (the daily lists are
   untouched), and spam_signatures over any stored Wayback excerpt and over
   the name split into words. No length, brandability or phase2 filters.
4. Picks the RDAP checks (2026-10-18 revision, after count-ranked top-50s
   filled with link networks and gambling sites two days running):
   - candidates with count >= `rebuild.min_cc_domains`;
   - `rebuild.cluster_min_size` or more with the EXACT same count are one link
     network: one is kept, the cluster is logged;
   - `rebuild.max_rdap_checks` is split into equal shares over
     `rebuild.rdap_bands` (20-49, 50-99, 100-499, 500+), highest count first
     within a band, unused shares passed to the other bands.
   RDAP runs through pipeline.validate_availability: the same rdap enricher,
   per-host buckets and throttles as the daily run, but a strike limit of 1.
   Any 429/403 is listed in the summary with its Retry-After and logged as
   an "RDAP REBUILD PUSHBACK" WARNING saying whether the cooldown outlasts
   the next daily run.
5. Buckets every checked domain by lifecycle, because leaving the zone is
   usually the START of expiry, not release: available (404), redemption,
   pending_delete, registrar_hold_or_grace, active, unknown. Each gets an
   expected release date where the RDAP events allow (a deletion event, else
   last changed + `rebuild.lifecycle_days`). Every row, with status values,
   registrar, expiration and events, goes to `rebuild.rdap_output_path`.
6. For every candidate in available, pending_delete or redemption, downloads
   the release's raw vertices + edges from R2 into a temp dir and lets DuckDB
   STREAM them: one pass over vertices for the candidates' ids, one pass over
   edges keeping only edges into those ids, one more pass over vertices to
   name the referrers. The graph is never loaded into memory; DuckDB's
   memory limit is cc_refresh's. Nothing is downloaded when there is no
   candidate. Writes `rebuild.output_path` (default
   scripts/state/rebuild_referrers_latest.jsonl), one line per candidate:

       {"name": "marketglow.com", "cc_source_domain_count": 27,
        "release": "cc-main-2026-jul-aug-sep", "bucket": "redemption",
        "registrar": "...", "rdap_status": [...], "expiration_date": "...",
        "last_changed": "...", "expected_release_date": "2026-11-14",
        "expected_release_basis": "last_changed+35d",
        "referrers_total": 27, "referrers": ["tideblock.org", ...]}

   `referrers` holds up to `rebuild.max_referrers`, the referring domains
   with the most hosts (CC num_hosts, a size proxy) first. Platforms such as
   blogspot.com therefore sort high; step 2 handles them. `referrers_total`
   excludes self-links, so it can be one below the SQLite count.

The last log line is `Rebuild summary: {...}` (JSON) with every count above.
The output file is private (scripts/state/ is gitignored) and is overwritten
each run.

Reuses cc_refresh's R2, release, disk and blackout helpers, cc_backlinks'
derived-SQLite cache, filter.py's keyword matchers, spam_signatures,
toxic_denylist and pipeline.validate_availability.

Exit codes: 0 done (including "nothing to do"), 1 failed,
2 refused by the blackout / pipeline-active guard.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import logging
import re
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from scripts import cc_refresh

logger = logging.getLogger(__name__)

_FILE_KINDS = ("vertices", "edges")
# SQLite's bound-parameter limit is 32766 on current builds but 999 on old
# ones; 900 per query is safe everywhere and still fast with the index.
_SQLITE_CHUNK = 900


# ---------------------------------------------------------------------------
# Config and guard
# ---------------------------------------------------------------------------


def _rebuild_config(config: dict) -> dict:
    block = config.get("rebuild")
    return block if isinstance(block, dict) else {}


def _blocked_reason(
    config: dict,
    *,
    now: datetime | None = None,
    unit_active_fn: Callable[[str], bool] | None = None,
) -> str | None:
    """Why this run must not start now, or None. Same window and unit as
    cc_refresh: the daily pipeline and the archive timer own the box then."""
    refresh = cc_refresh._refresh_config(config)
    now = now or datetime.now(timezone.utc)
    start = int(refresh.get("blackout_start_utc_hour", 0))
    end = int(refresh.get("blackout_end_utc_hour", 0))
    if cc_refresh._in_blackout(now.hour, start, end):
        return (
            f"inside the blackout window [{start:02d}:00, {end:02d}:00) UTC "
            f"(now {now.hour:02d}:xx UTC)"
        )
    unit = str(refresh.get("pipeline_unit", "") or "")
    if unit and (unit_active_fn or cc_refresh._systemd_unit_active)(unit):
        return f"{unit} is currently active"
    return None


# ---------------------------------------------------------------------------
# Input and counts
# ---------------------------------------------------------------------------


def load_drop_names(path: Path) -> list[str]:
    """Distinct lowercase names from the drops handoff, in file order.
    Corrupt lines are skipped with a warning; a missing file raises."""
    names: list[str] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("%s:%d is not JSON (%s); skipped", path, lineno, exc)
                continue
            name = str(obj.get("name") or "").strip().lower() if isinstance(obj, dict) else ""
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def cc_counts(sqlite_path: Path, names: list[str]) -> dict[str, int]:
    """{name: source_domain_count} for every name present in cc_apex."""
    counts: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        for i in range(0, len(names), _SQLITE_CHUNK):
            chunk = names[i:i + _SQLITE_CHUNK]
            marks = ",".join("?" * len(chunk))
            for apex, count in conn.execute(
                f"SELECT apex_domain, source_domain_count FROM cc_apex "
                f"WHERE apex_domain IN ({marks})",
                chunk,
            ):
                counts[apex] = int(count)
    finally:
        conn.close()
    return counts


def at_least(counts: list[int], buckets: list[int]) -> dict[str, int]:
    return {str(b): sum(1 for c in counts if c >= b) for b in buckets}


# ---------------------------------------------------------------------------
# Abuse filters
# ---------------------------------------------------------------------------


def _name_as_excerpt(name: str) -> dict:
    """The name split into words, shaped as an excerpt so spam_signatures'
    word-boundary scan can read it (casino-royale.net -> 'casino royale net').
    Concatenated words (casinoroyale) are left to the keyword substrings."""
    return {"title": " ".join(w for w in re.split(r"[^a-z]+", name.lower()) if w)}


def matched_extra_term(name: str, config: dict) -> str | None:
    """Rebuild-only gambling terms (`rebuild.extra_rejected_terms`); the daily
    keyword lists are not touched.

    `substrings` match anywhere in the apex label (merkurslots, bingoloft).
    `tokens` match only a whole token as filter.py splits names, on dots,
    hyphens and digit/letter boundaries: so "wins" catches voodoo-wins.org
    but not twins.org or voodoowins.org, and "bet" catches bet-365.com but
    not alphabet.com.
    """
    from scripts import filter as filter_mod

    terms = _rebuild_config(config).get("extra_rejected_terms") or {}
    apex = name.split(".", 1)[0].lower()
    for term in terms.get("substrings", []) or []:
        if term and term.lower() in apex:
            return term.lower()
    coarse, fine = filter_mod._tokenize(name)
    tokens = coarse | fine
    for term in terms.get("tokens", []) or []:
        if term and term.lower() in tokens:
            return term.lower()
    return None


def abuse_reason(
    name: str,
    config: dict,
    *,
    denylist: set[str],
    excerpts: dict[str, Any],
) -> str | None:
    """Why `name` fails the abuse filters, or None if it passes."""
    from scripts import filter as filter_mod
    from scripts.spam_signatures import scan_excerpt

    if name in denylist:
        return "toxic_denylist"
    if filter_mod._matched_hard_keyword(
        name,
        config.get("rejected_keywords", []) or [],
        config.get("rejected_keyword_prefixes", []) or [],
    ) or filter_mod._matched_substring_keyword(
        name, config.get("rejected_keyword_substrings", []) or [],
    ):
        return "rejected_keyword"
    if matched_extra_term(name, config):
        return "extra_rejected_term"
    stored = excerpts.get(name)
    if isinstance(stored, dict) and scan_excerpt(stored, config):
        return "spam_signature_excerpt"
    if scan_excerpt(_name_as_excerpt(name), config):
        return "spam_signature_name"
    return None


def _load_excerpts(config: dict) -> dict[str, Any]:
    """The Wayback excerpt sidecar, or {} when it is missing or unreadable."""
    raw = config.get("sidecar_excerpts_path", "src/data/wayback_excerpts.json")
    path = Path(raw) if Path(raw).is_absolute() else cc_refresh._REPO_ROOT / raw
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Excerpt sidecar %s unavailable (%s); name-only signature scan", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ---------------------------------------------------------------------------
# Release selection (R2 listing via cc_refresh helpers)
# ---------------------------------------------------------------------------


def retained_releases(s3: Any, bucket: str) -> list[str]:
    """Releases with BOTH raw files under cc/raw/, oldest first. Names that
    are not canonical release names are ignored."""
    files: dict[str, set[str]] = {}
    for key in cc_refresh._list_keys(s3, bucket, "cc/raw/"):
        parts = key.split("/")
        if len(parts) != 4:
            continue
        for kind in _FILE_KINDS:
            if key == cc_refresh._r2_raw_key(parts[2], kind):
                files.setdefault(parts[2], set()).add(kind)
    complete = [
        release for release, kinds in files.items()
        if kinds == set(_FILE_KINDS) and cc_refresh._parse_release(release) is not None
    ]
    return sorted(complete, key=cc_refresh._release_sort_key)


def choose_release(retained: list[str], config: dict) -> str | None:
    """The configured latest release when its raw is retained, else the
    newest retained one, else None."""
    preferred = cc_refresh._cc_config(config).get("latest_release")
    if preferred in retained:
        return preferred
    return retained[-1] if retained else None


# ---------------------------------------------------------------------------
# Selection: collapse link networks, then pick the RDAP checks by band
# ---------------------------------------------------------------------------


def collapse_clusters(cands: dict[str, int], min_size: int) -> tuple[dict[str, int], list[dict]]:
    """Treat `min_size`+ candidates with the EXACT same count as one link
    network: keep the first by name, drop the rest. Returns (kept, clusters),
    clusters largest first. On 2026-10-18, 38 kadin*.org domains sat at
    1,549 and filled most of the count-ranked top 50."""
    by_count: dict[int, list[str]] = {}
    for name, count in cands.items():
        by_count.setdefault(count, []).append(name)
    kept: dict[str, int] = {}
    clusters: list[dict] = []
    for count, members in by_count.items():
        members.sort()
        if min_size > 0 and len(members) >= min_size:
            kept[members[0]] = count
            clusters.append({
                "cc_source_domain_count": count, "size": len(members),
                "kept": members[0], "sample": members[:5],
            })
        else:
            kept.update({m: count for m in members})
    clusters.sort(key=lambda c: (-c["size"], -c["cc_source_domain_count"]))
    return kept, clusters


def _band_label(low: int, high: int | None) -> str:
    return f"{low}+" if high is None else f"{low}-{high}"


def select_by_band(
    cands: dict[str, int],
    bands: list[list[int | None]],
    cap: int,
) -> tuple[list[str], dict[str, dict[str, int]]]:
    """Equal shares of `cap` per band, highest count first within a band.

    A band with fewer candidates than its share gives the unused slots to the
    other bands, round-robin in band order, so the full cap is used whenever
    there are enough candidates. Returns (names in band order, per-band
    {eligible, selected})."""
    members: list[list[str]] = []
    for low, high in bands:
        in_band = [n for n, c in cands.items() if c >= int(low) and (high is None or c <= int(high))]
        members.append(sorted(in_band, key=lambda n: (-cands[n], n)))
    if not members or cap <= 0:
        return [], {}
    base, extra = divmod(cap, len(members))
    take = [min(len(m), base + (1 if i < extra else 0)) for i, m in enumerate(members)]
    leftover = cap - sum(take)
    while leftover > 0:
        progressed = False
        for i, m in enumerate(members):
            if leftover and take[i] < len(m):
                take[i] += 1
                leftover -= 1
                progressed = True
        if not progressed:
            break
    selected = [n for i, m in enumerate(members) for n in m[:take[i]]]
    stats = {
        _band_label(*band): {"eligible": len(members[i]), "selected": take[i]}
        for i, band in enumerate(bands)
    }
    return selected, stats


# ---------------------------------------------------------------------------
# Lifecycle: what "left the zone" turned into
# ---------------------------------------------------------------------------

CANDIDATE_BUCKETS = ("available", "pending_delete", "redemption")


def _norm_status(values: list[str]) -> set[str]:
    """RDAP 'pending delete' and EPP 'pendingDelete' both -> 'pendingdelete'."""
    return {str(v).replace(" ", "").replace("_", "").lower() for v in values or []}


def _event_dates(events: Any) -> dict[str, str]:
    """{eventAction: YYYY-MM-DD}, first occurrence of each action. Accepts raw
    RDAP events (eventAction/eventDate) and stored ones (action/date)."""
    out: dict[str, str] = {}
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        action = ev.get("eventAction") or ev.get("action")
        when = ev.get("eventDate") or ev.get("date")
        if isinstance(action, str) and isinstance(when, str) and len(when) >= 10:
            out.setdefault(action.lower(), when[:10])
    return out


def classify_lifecycle(result: dict, today: date, config: dict) -> dict:
    """Bucket one RDAP result and estimate when the domain is released.

    Buckets: available (RDAP 404); redemption (redemption period or pending
    restore); pending_delete; registrar_hold_or_grace (client/server hold,
    auto-renew period, or expired but still held by the registrar, the usual
    window for registrar auctions); active; unknown (no RDAP answer).

    Expected release: a "deletion" event when the registry publishes one;
    otherwise "last changed" + rebuild.lifecycle_days (5 days pendingDelete;
    30 + 5 from redemption), on the assumption that the last change was
    entry into that status. Basis is returned with every date.
    """
    days = _rebuild_config(config).get("lifecycle_days") or {}
    pending_days = int(days.get("pending_delete", 5))
    redemption_days = int(days.get("redemption", 30))
    status = _norm_status(result.get("rdap_status") or [])
    events = _event_dates(result.get("events"))
    expiration = result.get("rdap_expiration") or events.get("expiration")

    if result.get("is_available") is True:
        bucket = "available"
    elif result.get("is_available") is None:
        bucket = "unknown"
    elif status & {"redemptionperiod", "pendingrestore"}:
        bucket = "redemption"
    elif "pendingdelete" in status:
        bucket = "pending_delete"
    elif status & {"clienthold", "serverhold", "autorenewperiod"} or (
        isinstance(expiration, str) and expiration[:10] < today.isoformat()
    ):
        bucket = "registrar_hold_or_grace"
    else:
        bucket = "active"

    release: str | None = None
    basis: str | None = None
    if bucket == "available":
        release, basis = today.isoformat(), "rdap_404"
    elif bucket in ("pending_delete", "redemption"):
        if "deletion" in events:
            release, basis = events["deletion"], "deletion_event"
        elif "last changed" in events:
            add = pending_days + (redemption_days if bucket == "redemption" else 0)
            release = (date.fromisoformat(events["last changed"]) + timedelta(days=add)).isoformat()
            basis = f"last_changed+{add}d"
        else:
            basis = "no_dated_event"
    return {
        "bucket": bucket,
        "expected_release_date": release,
        "expected_release_basis": basis,
        "last_changed": events.get("last changed"),
        "expiration_date": expiration[:10] if isinstance(expiration, str) else None,
    }


# ---------------------------------------------------------------------------
# RDAP
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture_rdap_records():
    """Keep the JSON body of every RDAP 200 during the rebuild pass.

    rdap.check_availability returns status, registrar and expiration but not
    the events list, and rdap.py is daily-pipeline code this branch must not
    change. So the pass wraps rdap._rdap_get, the one function that does the
    HTTP GET (looked up at call time), and restores it afterwards. Throttling,
    429 handling and the request itself are untouched."""
    from scripts.enrichment import rdap

    records: dict[str, dict] = {}
    lock = threading.Lock()
    original = rdap._rdap_get

    def capturing(url, rdap_host, timeout, config):
        resp = original(url, rdap_host, timeout, config)
        if getattr(resp, "status_code", None) == 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                with lock:
                    records[url.rstrip("/").rsplit("/", 1)[-1].lower()] = body
        return resp

    rdap._rdap_get = capturing
    try:
        yield records
    finally:
        rdap._rdap_get = original


class _RateLimitCapture(logging.Handler):
    """Collects the circuit breaker's 429 records during the rebuild RDAP pass.

    The breaker keeps no record of the Retry-After value it honoured, only a
    cooldown expiry, but its "429 from <host> ..." WARNING carries
    (host, strikes, strike_limit, cooldown_seconds, raw_header) as log
    arguments. Reading the arguments, not the message text, keeps this
    independent of the wording.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            args = record.args
            if str(record.msg).startswith("429 from ") and isinstance(args, tuple) and len(args) == 5:
                host, _strikes, _limit, cooldown, raw = args
                self.events.append({
                    "host": str(host),
                    "status": 429,
                    "retry_after_header": raw,
                    "honored_cooldown_seconds": round(float(cooldown)),
                })
        except Exception:  # pragma: no cover — capture must never break the pass
            pass


def check_available(names: list[str], config: dict, budget_seconds: float) -> dict[str, Any]:
    """RDAP through the daily run's own path. Returns the available names, the
    per-outcome counts, and every host that pushed back (`rate_limited`).

    Stricter than the daily run: the strike limit is 1, so a host is stopped
    for the rest of this pass on its FIRST 429. This pass runs in the
    afternoon against the same registries as the 09:00 run, and a long
    Retry-After ban (identitydigital once sent 86397s) started now would
    still be running then; the cooldown is per-process, so the morning run
    would not know. Hard rule 20.
    """
    from scripts import pipeline
    from scripts.enrichment._circuit_breaker import GLOBAL_HOST_STOP

    rdap_config = copy.deepcopy(config)
    rdap_config["availability_budget_seconds"] = budget_seconds
    rdap_config["rdap_429_strike_limit"] = 1
    candidates = [{"name": n, "tld": n.rsplit(".", 1)[-1]} for n in names]

    capture = _RateLimitCapture()
    breaker_logger = logging.getLogger("scripts.enrichment._circuit_breaker")
    breaker_logger.addHandler(capture)
    try:
        with _capture_rdap_records() as records:
            available = pipeline.validate_availability(candidates, rdap_config)
    finally:
        breaker_logger.removeHandler(capture)

    # Every checked domain's full result: what the enricher returned plus the
    # events list from the raw record (registered domains only).
    results: dict[str, dict] = {}
    for c in candidates:
        raw = records.get(c["name"]) or {}
        results[c["name"]] = {
            "is_available": c.get("is_available"),
            "rdap_http": c.get("rdap_http"),
            "rdap_status": c.get("rdap_status") or [],
            "registrar": c.get("previous_registrar"),
            "rdap_expiration": c.get("rdap_expiration"),
            "events": [
                {"action": ev.get("eventAction"), "date": str(ev.get("eventDate"))[:10]}
                for ev in raw.get("events", []) or []
                if isinstance(ev, dict) and ev.get("eventAction") and ev.get("eventDate")
            ],
        }

    rate_limited = list(capture.events)
    seen = {e["host"] for e in rate_limited}
    for host, (_when, reason) in GLOBAL_HOST_STOP.stopped_hosts().items():
        if reason == "403" and host not in seen:
            rate_limited.append({
                "host": host, "status": 403,
                "retry_after_header": None, "honored_cooldown_seconds": None,
            })
    return {
        "available": [c["name"] for c in available],
        "not_available": sum(1 for c in candidates if c.get("is_available") is False),
        "unknown": sum(1 for c in candidates if c.get("is_available") is None),
        "rate_limited": rate_limited,
        "results": results,
    }


def _warn_rate_limited(events: list[dict[str, Any]], config: dict, *, now: datetime | None = None) -> None:
    """One WARNING per host that pushed back, saying whether its cooldown
    still runs at the next daily run."""
    now = now or datetime.now(timezone.utc)
    hour = int(_rebuild_config(config).get("daily_run_utc_hour", 9))
    next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    for event in events:
        if event["status"] == 403:
            logger.warning(
                "RDAP REBUILD PUSHBACK: %s returned HTTP 403 (outright block, not a "
                "rate limit) during the rebuild RDAP pass. The next daily run at "
                "%s UTC will hit the same host. Investigate before then.",
                event["host"], next_run.strftime("%Y-%m-%d %H:%M"),
            )
            continue
        until = now + timedelta(seconds=event["honored_cooldown_seconds"] or 0)
        logger.warning(
            "RDAP REBUILD PUSHBACK: %s returned HTTP 429 during the rebuild RDAP "
            "pass (Retry-After header=%r, honoured %ss, until ~%s UTC); host stopped "
            "for the rest of the pass. %s",
            event["host"], event["retry_after_header"], event["honored_cooldown_seconds"],
            until.strftime("%Y-%m-%d %H:%M"),
            (
                f"STILL ACTIVE at the next daily run ({next_run.strftime('%Y-%m-%d %H:%M')} UTC): "
                "that run will hit this host during the registry's stated cooldown."
                if until > next_run else
                f"Ends before the next daily run ({next_run.strftime('%Y-%m-%d %H:%M')} UTC)."
            ),
        )


# ---------------------------------------------------------------------------
# The streaming referrer query
# ---------------------------------------------------------------------------


def _flip(domain: str) -> str:
    """example.com <-> com.example (CC stores domains reversed)."""
    return ".".join(reversed(domain.split(".")))


def find_referrers(
    vertices_path: Path,
    edges_path: Path,
    names: list[str],
    max_referrers: int,
) -> dict[str, tuple[int, list[str]]]:
    """{apex: (referrers_total, [top referrers])} for every apex found in the
    graph. Apexes not in the vertices file are absent from the result.

    DuckDB scans the gzipped TSVs in a streaming fashion; only the target
    apexes' ids, their inbound edges and those referrers' names are kept.
    """
    import duckdb  # lazy, as in cc_refresh

    vertex_cols = "{'id': 'INTEGER', 'reversed_domain': 'VARCHAR', 'num_hosts': 'INTEGER'}"
    edge_cols = "{'from_id': 'INTEGER', 'to_id': 'INTEGER'}"
    vertices = f"read_csv(?, delim='\t', header=false, columns={vertex_cols})"
    edges = f"read_csv(?, delim='\t', header=false, columns={edge_cols})"

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"SET memory_limit='{cc_refresh._DUCKDB_MEMORY_LIMIT}'")
        con.execute("CREATE TEMP TABLE targets (reversed_domain VARCHAR)")
        con.executemany(
            "INSERT INTO targets VALUES (?)", [[_flip(n)] for n in names],
        )

        started = time.monotonic()
        con.execute(
            f"""
            CREATE TEMP TABLE target_v AS
            SELECT id, reversed_domain FROM {vertices}
            WHERE reversed_domain IN (SELECT reversed_domain FROM targets)
            """,
            [str(vertices_path)],
        )
        found = con.execute("SELECT COUNT(*) FROM target_v").fetchone()[0]
        logger.info(
            "Vertices pass: %d of %d apexes are in the graph (%.1fs)",
            found, len(names), time.monotonic() - started,
        )
        if not found:
            return {}

        started = time.monotonic()
        con.execute(
            f"""
            CREATE TEMP TABLE hits AS
            SELECT DISTINCT from_id, to_id FROM {edges}
            WHERE to_id IN (SELECT id FROM target_v) AND from_id <> to_id
            """,
            [str(edges_path)],
        )
        edge_count = con.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
        logger.info(
            "Edges pass: %d inbound edges to the apexes (%.1fs)",
            edge_count, time.monotonic() - started,
        )

        started = time.monotonic()
        con.execute(
            f"""
            CREATE TEMP TABLE ref_v AS
            SELECT id, reversed_domain, num_hosts FROM {vertices}
            WHERE id IN (SELECT from_id FROM hits)
            """,
            [str(vertices_path)],
        )
        logger.info("Referrer names pass (%.1fs)", time.monotonic() - started)

        rows = con.execute(
            """
            SELECT target, referrer, total FROM (
                SELECT t.reversed_domain AS target,
                       r.reversed_domain AS referrer,
                       COUNT(*) OVER (PARTITION BY t.id) AS total,
                       row_number() OVER (
                           PARTITION BY t.id
                           ORDER BY r.num_hosts DESC, r.reversed_domain
                       ) AS rn
                FROM hits h
                JOIN target_v t ON h.to_id = t.id
                JOIN ref_v r ON h.from_id = r.id
            ) WHERE rn <= ?
            ORDER BY target, rn
            """,
            [max_referrers],
        ).fetchall()
        result: dict[str, tuple[int, list[str]]] = {}
        for target_rev, referrer_rev, total in rows:
            _, refs = result.setdefault(_flip(target_rev), (int(total), []))
            refs.append(_flip(referrer_rev))
        for rev_name, in con.execute("SELECT reversed_domain FROM target_v").fetchall():
            result.setdefault(_flip(rev_name), (0, []))
        return result
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _peak_rss_mib() -> float | None:
    """Peak resident memory of this process (DuckDB runs in-process)."""
    try:
        import resource
    except ImportError:  # Windows dev box
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _write_output(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    cc_refresh._atomic_write_text(path, text)


def run(
    config: dict,
    *,
    input_path: Path,
    output_path: Path,
    min_cc_domains: int,
    release: str | None,
    s3_factory: Callable[[], tuple[Any, str]],
    sqlite_path_fn: Callable[[str, Any, str], Path] | None = None,
    denylist_fn: Callable[[Any, str], set[str]] | None = None,
    rdap_fn: Callable[[list[str], dict, float], dict[str, Any]] | None = None,
) -> int:
    started = time.monotonic()
    rcfg = _rebuild_config(config)
    max_referrers = int(rcfg.get("max_referrers", 100))
    max_rdap = int(rcfg.get("max_rdap_checks", 50))
    rdap_budget = float(rcfg.get("rdap_budget_seconds", 3600))
    buckets = [int(b) for b in rcfg.get("distribution_buckets", [1, 5, 10, 20, 50, 100])]
    summary: dict[str, Any] = {"min_cc_domains": min_cc_domains}

    names = load_drop_names(input_path)
    summary["drops"] = len(names)
    logger.info("Loaded %d distinct drops from %s", len(names), input_path)

    s3, bucket = s3_factory()
    retained = retained_releases(s3, bucket)
    logger.info("Raw releases retained in r2://%s/cc/raw/: %s", bucket, retained or "none")
    if release is None:
        release = choose_release(retained, config)
    elif release not in retained:
        logger.error("Release %s has no complete raw in R2 (retained: %s)", release, retained)
        return 1
    if release is None:
        logger.error("No release with both raw files is retained in R2; nothing to read")
        return 1
    summary["release"] = release

    # 2. counts from the derived SQLite of the same release
    if sqlite_path_fn is None:
        from scripts.enrichment import cc_backlinks

        def sqlite_path_fn(rel: str, client: Any, bkt: str) -> Path:
            return cc_backlinks._ensure_local_sqlite(
                rel, config, s3_client=client, bucket=bkt,
            )

    counts = cc_counts(sqlite_path_fn(release, s3, bucket), names)
    summary["in_graph"] = len(counts)
    summary["drops_at_least"] = at_least(list(counts.values()), buckets)
    logger.info(
        "Counts from %s derived SQLite: %d of %d drops in the graph; at least N linking "
        "domains: %s", release, len(counts), len(names), summary["drops_at_least"],
    )

    # 3. abuse filters on everything in the graph (cheap), reported per bucket
    denylist = (denylist_fn or _default_denylist)(s3, bucket)
    excerpts = _load_excerpts(config)
    reasons: dict[str, int] = {}
    passed: dict[str, int] = {}
    for name, count in counts.items():
        reason = abuse_reason(name, config, denylist=denylist, excerpts=excerpts)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            passed[name] = count
    summary["abuse_rejections"] = dict(sorted(reasons.items()))
    summary["abuse_pass_at_least"] = at_least(list(passed.values()), buckets)
    logger.info(
        "Abuse filters: rejected %s; passing, at least N linking domains: %s",
        summary["abuse_rejections"], summary["abuse_pass_at_least"],
    )

    # 4. collapse link networks, then pick the RDAP checks by count band
    gated = {n: c for n, c in passed.items() if c >= min_cc_domains}
    summary["rdap_gated"] = len(gated)
    kept, clusters = collapse_clusters(gated, int(rcfg.get("cluster_min_size", 3)))
    summary["clusters_collapsed"] = len(clusters)
    summary["cluster_members_dropped"] = len(gated) - len(kept)
    summary["clusters"] = [
        {k: c[k] for k in ("cc_source_domain_count", "size", "kept")} for c in clusters
    ]
    for c in clusters:
        logger.info(
            "Cluster: %d candidates share cc_source_domain_count=%d; kept %s (e.g. %s)",
            c["size"], c["cc_source_domain_count"], c["kept"], ", ".join(c["sample"]),
        )
    bands = rcfg.get("rdap_bands") or [[20, 49], [50, 99], [100, 499], [500, None]]
    to_check, band_stats = select_by_band(kept, bands, max_rdap)
    summary["rdap_bands"] = band_stats
    summary["rdap_checked"] = len(to_check)
    summary["rdap_not_checked_cap"] = len(kept) - len(to_check)
    logger.info(
        "RDAP: %d gated, %d after collapsing %d cluster(s); checking %d by band %s "
        "(cap %d, budget %.0fs)",
        len(gated), len(kept), len(clusters), len(to_check), band_stats, max_rdap, rdap_budget,
    )
    rdap = (rdap_fn or check_available)(to_check, config, rdap_budget) if to_check else {
        "available": [], "not_available": 0, "unknown": 0, "results": {},
    }
    summary["rdap_available"] = len(rdap["available"])
    summary["rdap_not_available"] = rdap["not_available"]
    summary["rdap_unknown"] = rdap["unknown"]
    summary["rdap_rate_limited"] = rdap.get("rate_limited", [])
    if summary["rdap_rate_limited"]:
        _warn_rate_limited(summary["rdap_rate_limited"], config)

    # 5. lifecycle bucket and expected release date for every checked domain
    today = datetime.now(timezone.utc).date()
    rdap_rows: list[dict] = []
    for name in to_check:
        result = (rdap.get("results") or {}).get(name) or {
            "is_available": True if name in rdap["available"] else None,
        }
        life = classify_lifecycle(result, today, config)
        rdap_rows.append({
            "name": name,
            "cc_source_domain_count": kept[name],
            "band": next((label for label, (low, high) in zip(band_stats, bands)
                          if kept[name] >= int(low) and (high is None or kept[name] <= int(high))), None),
            **life,
            "rdap_status": result.get("rdap_status") or [],
            "registrar": result.get("registrar"),
            "events": result.get("events") or [],
        })
    buckets_count: dict[str, int] = {}
    for row in rdap_rows:
        buckets_count[row["bucket"]] = buckets_count.get(row["bucket"], 0) + 1
    summary["rdap_buckets"] = dict(sorted(buckets_count.items()))
    rdap_output = output_path.with_name(Path(str(rcfg.get(
        "rdap_output_path", "scripts/state/rebuild_rdap_latest.jsonl"))).name)
    _write_output(rdap_output, rdap_rows)
    logger.info("RDAP lifecycle buckets %s; full results written to %s", summary["rdap_buckets"], rdap_output)

    candidates = sorted(
        (r for r in rdap_rows if r["bucket"] in CANDIDATE_BUCKETS),
        key=lambda r: (CANDIDATE_BUCKETS.index(r["bucket"]), -r["cc_source_domain_count"], r["name"]),
    )
    summary["candidates"] = len(candidates)

    # 6. referrers for every candidate (available, pending_delete, redemption)
    if not candidates:
        _write_output(output_path, [])
        summary["temp_disk_gib"] = 0.0
        _log_summary(summary, started)
        logger.info("No candidate in %s; wrote an empty %s and skipped the raw download",
                    "/".join(CANDIDATE_BUCKETS), output_path)
        return 0

    sizes = {
        kind: cc_refresh._r2_object_exists(s3, bucket, cc_refresh._r2_raw_key(release, kind))[1]
        for kind in _FILE_KINDS
    }
    summary["temp_disk_gib"] = round(sum(sizes.values()) / 1024**3, 2)
    workdir_cfg = str(rcfg.get("workdir") or "")
    workdir = Path(workdir_cfg) if workdir_cfg else Path(tempfile.gettempdir())
    workdir.mkdir(parents=True, exist_ok=True)
    headroom = float(rcfg.get("disk_headroom_multiplier", 1.5))
    cc_refresh._ensure_disk_space(workdir, int(sum(sizes.values()) * headroom))

    with tempfile.TemporaryDirectory(prefix="rebuild-referrers-", dir=str(workdir)) as tmp:
        paths = {kind: Path(tmp) / f"{kind}.txt.gz" for kind in _FILE_KINDS}
        download_started = time.monotonic()
        for kind in _FILE_KINDS:
            cc_refresh._download_from_r2(
                s3, bucket, cc_refresh._r2_raw_key(release, kind), paths[kind],
            )
        logger.info(
            "Downloaded raw %s (%.2f GiB) in %.1fs",
            release, sum(sizes.values()) / 1024**3, time.monotonic() - download_started,
        )
        found = find_referrers(
            paths["vertices"], paths["edges"], [r["name"] for r in candidates], max_referrers,
        )

    out = []
    for row in candidates:
        total, refs = found.get(row["name"], (0, []))
        out.append({
            "name": row["name"],
            "cc_source_domain_count": row["cc_source_domain_count"],
            "release": release,
            "bucket": row["bucket"],
            "registrar": row["registrar"],
            "rdap_status": row["rdap_status"],
            "expiration_date": row["expiration_date"],
            "last_changed": row["last_changed"],
            "expected_release_date": row["expected_release_date"],
            "expected_release_basis": row["expected_release_basis"],
            "referrers_total": total,
            "referrers": refs,
        })
    _write_output(output_path, out)
    summary["written"] = len(out)
    logger.info("Wrote %d candidate(s) to %s", len(out), output_path)
    _log_summary(summary, started)
    return 0


def _default_denylist(s3: Any, bucket: str) -> set[str]:
    from scripts import toxic_denylist

    return toxic_denylist.load_denylist(r2_client=s3, r2_bucket=bucket)


def _log_summary(summary: dict[str, Any], started: float) -> None:
    summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    peak = _peak_rss_mib()
    summary["peak_rss_mib"] = round(peak) if peak is not None else None
    logger.info("Rebuild summary: %s", json.dumps(summary, sort_keys=True))


def main(
    argv: list[str] | None = None,
    *,
    s3_factory: Callable[[], tuple[Any, str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--config", default=str(cc_refresh._DEFAULT_CONFIG_PATH))
    parser.add_argument("--input", help="override rebuild.input_path")
    parser.add_argument("--output", help="override rebuild.output_path")
    parser.add_argument("--min-cc-domains", type=int, help="override rebuild.min_cc_domains")
    parser.add_argument("--release", help="use this retained release instead of the default")
    parser.add_argument(
        "--force", action="store_true",
        help="run even inside the blackout window or while the pipeline is active",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    config = cc_refresh._load_config(args.config)
    rcfg = _rebuild_config(config)

    if not args.force:
        reason = _blocked_reason(config)
        if reason:
            logger.error("Refusing to run: %s. Use --force to override.", reason)
            return 2

    def _default_s3() -> tuple[Any, str]:
        from scripts import diff

        return diff._r2_client(), diff._bucket()

    repo = cc_refresh._REPO_ROOT

    def _path(override: str | None, key: str, default: str) -> Path:
        p = Path(override or rcfg.get(key) or default)
        return p if p.is_absolute() else repo / p

    try:
        return run(
            config,
            input_path=_path(args.input, "input_path", "scripts/state/drops_latest.jsonl"),
            output_path=_path(args.output, "output_path", "scripts/state/rebuild_referrers_latest.jsonl"),
            min_cc_domains=(
                args.min_cc_domains if args.min_cc_domains is not None
                else int(rcfg.get("min_cc_domains", 20))
            ),
            release=args.release,
            s3_factory=s3_factory or _default_s3,
        )
    except Exception:
        logger.exception("rebuild_referrers failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
