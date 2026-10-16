"""Read-only HTTP API over the DomainSifter snapshot, for Asset X-Ray.

Serves https://api.domainsifter.com/v1 through a Cloudflare Tunnel to
`api.bind_host:api.port` (127.0.0.1:8010). Standard library only.

ISOLATION. This process reads ONE file, the snapshot written by
scripts/api_snapshot.py, plus scripts/config.json for its settings and the
provenance registry. It has no R2 credentials, never reads the repo's .env,
and exposes no write path: every route is GET. Error bodies never carry
exception text, file paths or host names.

AUTH. `Authorization: Bearer <token>` against DOMAINSIFTER_API_TOKENS
(comma-separated, so two tokens can be live during a rotation). The server
refuses to start with no token configured.

Contract: docs/api/data-dictionary.md and GET /v1/openapi.json.

    python -m scripts.api_server
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

logger = logging.getLogger(__name__)

API_VERSION = "1.0.0"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "scripts" / "config.json"
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
_SORTS = ("first_seen_desc", "referring_domains_desc", "wayback_first_year_asc")
_RELOAD_CHECK_SECONDS = 5.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------


class SnapshotStore:
    """The snapshot in memory, reloaded when the file changes on disk.

    A failed reload keeps serving the previous snapshot. Thread-safe.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.monotonic):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._checked_at = float("-inf")
        self.data: dict | None = None
        self.index: dict[str, Any] = {}

    def current(self) -> dict | None:
        now = self._clock()
        with self._lock:
            if now - self._checked_at >= _RELOAD_CHECK_SECONDS:
                self._checked_at = now
                self._maybe_reload()
            return self.data

    def _maybe_reload(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self.data is None:
                logger.warning("Snapshot not found; serving 503 until it exists")
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("domains"), dict):
                raise ValueError("snapshot has no domains object")
        except (OSError, ValueError) as exc:
            logger.error("Snapshot reload failed (%s); keeping the previous one", type(exc).__name__)
            return
        self.data = data
        self.index = _build_index(data)
        self._mtime = mtime
        logger.info("Snapshot loaded: %d domains, built_at %s", len(data["domains"]), data.get("built_at"))


def _build_index(data: dict) -> dict[str, Any]:
    domains = data["domains"]
    last_by_source: dict[str, str] = {}
    for doc in domains.values():
        for obs in doc.get("observations", []):
            src, at = obs.get("source"), obs.get("observed_at")
            if src and at and at > last_by_source.get(src, ""):
                last_by_source[src] = at
        # Every drop_date comes from the daily CZDS zone diff, even when the
        # availability itself was RDAP-confirmed.
        drop = (doc.get("availability") or {}).get("drop_date")
        if drop:
            at = f"{drop}T00:00:00Z"
            if at > last_by_source.get("icann_czds", ""):
                last_by_source["icann_czds"] = at
    return {
        "by_first_seen": sorted(domains.values(), key=lambda d: (d["first_seen_at"] or "", d["domain"]), reverse=True),
        "by_changed": sorted(domains.values(), key=lambda d: (d["last_seen_at"] or "", d["domain"])),
        "last_by_source": last_by_source,
    }


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token bucket per credential."""

    def __init__(self, rate: float, burst: float, *, clock: Callable[[], float] = time.monotonic):
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> float:
        """0.0 if allowed, else the seconds until a request would be."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - last) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self._rate


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    name = unquote(raw).strip().lower().rstrip(".")
    try:
        name = name.encode("idna").decode("ascii") if not name.isascii() else name
    except UnicodeError:
        raise ApiError(400, "bad_request", "Domain is not a valid hostname.") from None
    if not _DOMAIN_RE.match(name):
        raise ApiError(400, "bad_request", "Domain is not a valid hostname.")
    return name


def _parse_time(raw: str, param: str) -> str:
    """ISO-8601 date or timestamp -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    value = raw.strip()
    try:
        if len(value) == 10:
            dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError(400, "bad_request", f"{param} must be an ISO-8601 date or UTC timestamp.") from None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _param(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[-1] if values else None


def _bool_param(query: dict[str, list[str]], name: str) -> bool | None:
    raw = _param(query, name)
    if raw is None:
        return None
    if raw.lower() in ("true", "1"):
        return True
    if raw.lower() in ("false", "0"):
        return False
    raise ApiError(400, "bad_request", f"{name} must be true or false.")


def _int_param(query: dict[str, list[str]], name: str, *, minimum: int = 0) -> int | None:
    raw = _param(query, name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, "bad_request", f"{name} must be an integer.") from None
    if value < minimum:
        raise ApiError(400, "bad_request", f"{name} must be >= {minimum}.")
    return value


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


class Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]):
        self.status = status
        self.body = body
        self.headers = headers


class Api:
    def __init__(
        self,
        config: dict,
        tokens: list[str],
        store: SnapshotStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not tokens:
            raise ValueError("no API token configured")
        api_cfg = config.get("api", {}) or {}
        self._tokens = [t.encode("utf-8") for t in tokens]
        self._sources = list(api_cfg.get("sources", []) or [])
        self._store = store
        self._default_limit = int(api_cfg.get("default_page_limit", 100))
        self._max_limit = int(api_cfg.get("max_page_limit", 1000))
        self._limiter = RateLimiter(
            float(api_cfg.get("rate_limit_per_second", 20)),
            float(api_cfg.get("rate_limit_burst", 40)),
            clock=clock,
        )

    # -- entry point -------------------------------------------------------

    def handle(self, method: str, target: str, headers: dict[str, str]) -> Response:
        try:
            return self._dispatch(method, target, {k.lower(): v for k, v in headers.items()})
        except ApiError as err:
            return self._error(err)
        except Exception:
            logger.exception("Unhandled error on %s %s", method, urlsplit(target).path)
            return self._error(ApiError(500, "internal", "Internal error."))

    def _dispatch(self, method: str, target: str, headers: dict[str, str]) -> Response:
        token = self._authenticate(headers)
        wait = self._limiter.allow(hashlib.sha256(token).hexdigest())
        if wait > 0:
            raise ApiError(429, "rate_limited", "Too many requests.",
                           {"Retry-After": str(max(1, int(wait + 0.999)))})
        if method != "GET":
            raise ApiError(405, "bad_request", "Only GET is supported.", {"Allow": "GET"})

        parts = urlsplit(target)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query, keep_blank_values=True)

        if path == "/v1/health":
            return self._json(self._health(), headers)
        if path == "/v1/sources":
            return self._json({"items": self._sources}, headers)
        if path == "/v1/openapi.json":
            return self._json(openapi_document(), headers)

        snapshot = self._snapshot()
        if path == "/v1/candidates":
            return self._json(self._candidates(snapshot, query), headers, snapshot)
        if path == "/v1/changes":
            return self._json(self._changes(snapshot, query), headers, snapshot)

        m = re.fullmatch(r"/v1/domains/([^/]+)(?:/(observations|backlinks|referring-domains))?", path)
        if m:
            domain = normalize_domain(m.group(1))
            sub = m.group(2)
            if sub == "backlinks":
                raise ApiError(501, "not_found", "Not implemented: DomainSifter holds no per-link backlink data.")
            if sub == "referring-domains":
                raise ApiError(501, "not_found", "Not implemented: DomainSifter serves referring-domain counts only (see metrics.referring_domains).")
            doc = snapshot["domains"].get(domain)
            if doc is None:
                raise ApiError(404, "not_found", "Domain not in DomainSifter.")
            if sub == "observations":
                return self._json(self._page(doc.get("observations", []), query, f"obs:{domain}"), headers, snapshot)
            return self._json({k: v for k, v in doc.items() if k != "observations"}, headers, snapshot)

        raise ApiError(404, "not_found", "No such endpoint.")

    # -- auth / snapshot ---------------------------------------------------

    def _authenticate(self, headers: dict[str, str]) -> bytes:
        auth = headers.get("authorization", "")
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() == "bearer" and presented.strip():
            candidate = presented.strip().encode("utf-8")
            if any(hmac.compare_digest(candidate, t) for t in self._tokens):
                return candidate
        raise ApiError(401, "unauthorized", "Missing or invalid bearer token.",
                       {"WWW-Authenticate": 'Bearer realm="domainsifter"'})

    def _snapshot(self) -> dict:
        data = self._store.current()
        if data is None:
            raise ApiError(503, "internal", "Data not available yet.", {"Retry-After": "60"})
        return data

    # -- endpoints ---------------------------------------------------------

    def _health(self) -> dict:
        data = self._store.current()
        if data is None:
            return {"status": "degraded", "version": API_VERSION, "data": None}
        index = self._store.index
        sha = data.get("git_sha")
        return {
            "status": "ok",
            "version": f"{API_VERSION}+{sha}" if sha else API_VERSION,
            "data": {
                "domains": len(data["domains"]),
                "last_ingest_at": data.get("built_at"),
                "sources": [
                    {"id": s.get("id"), "last_run_at": index["last_by_source"].get(s.get("id"))}
                    for s in self._sources
                ],
            },
        }

    def _candidates(self, snapshot: dict, query: dict[str, list[str]]) -> dict:
        if _param(query, "niche") is not None:
            raise ApiError(400, "bad_request", "niche is not available: DomainSifter does not classify niche (analysis.niche is always null).")
        sort = _param(query, "sort") or "first_seen_desc"
        if sort not in _SORTS:
            raise ApiError(400, "bad_request", f"sort must be one of {', '.join(_SORTS)}.")
        status = _param(query, "status")
        tld = (_param(query, "tld") or "").lower().lstrip(".") or None
        min_rd = _int_param(query, "min_referring_domains")
        has_wb = _bool_param(query, "has_wayback")
        first_after = _parse_time(_param(query, "first_seen_after"), "first_seen_after") if _param(query, "first_seen_after") else None
        updated_after = _parse_time(_param(query, "updated_after"), "updated_after") if _param(query, "updated_after") else None

        def keep(doc: dict) -> bool:
            if status and doc["availability"]["status"] != status:
                return False
            if tld and doc["tld"] != tld:
                return False
            if min_rd is not None:
                rd = _metric_value(doc, "referring_domains")
                if rd is None or rd < min_rd:
                    return False
            if has_wb is not None and ((doc["wayback"]["snapshots"] or 0) > 0) != has_wb:
                return False
            if first_after and not (doc["first_seen_at"] or "") > first_after:
                return False
            if updated_after and not (doc["last_seen_at"] or "") > updated_after:
                return False
            return True

        rows = [d for d in self._store.index["by_first_seen"] if keep(d)]
        if sort == "referring_domains_desc":
            rows.sort(key=lambda d: (_metric_value(d, "referring_domains") is None,
                                     -(_metric_value(d, "referring_domains") or 0), d["domain"]))
        elif sort == "wayback_first_year_asc":
            rows.sort(key=lambda d: (d["wayback"]["first_year"] is None, d["wayback"]["first_year"] or 0, d["domain"]))
        page = self._page(rows, query, "cand:" + json.dumps(sorted(
            (k, v) for k, v in query.items() if k not in ("cursor", "limit")
        )))
        page["items"] = [_compact(d) for d in page["items"]]
        return page

    def _changes(self, snapshot: dict, query: dict[str, list[str]]) -> dict:
        raw = _param(query, "since")
        if not raw:
            raise ApiError(400, "bad_request", "since is required.")
        since = _parse_time(raw, "since")
        rows = [d for d in self._store.index["by_changed"] if (d["last_seen_at"] or "") > since]
        page = self._page(rows, query, f"chg:{since}")
        page["items"] = [
            {
                "domain": d["domain"],
                "changed_at": d["last_seen_at"],
                "change": "added" if (d["first_seen_at"] or "") > since else "updated",
            }
            for d in page["items"]
        ]
        return page

    # -- pagination --------------------------------------------------------

    def _page(self, rows: list, query: dict[str, list[str]], scope: str) -> dict:
        limit = _int_param(query, "limit", minimum=1)
        limit = min(limit if limit is not None else self._default_limit, self._max_limit)
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        offset = 0
        cursor = _param(query, "cursor")
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                offset, scope_seen = int(decoded["o"]), decoded["s"]
            except Exception:
                raise ApiError(400, "bad_request", "Invalid cursor.") from None
            if scope_seen != scope_hash or offset < 0:
                raise ApiError(400, "bad_request", "Cursor does not belong to this query.")
        items = rows[offset:offset + limit]
        next_offset = offset + limit
        next_cursor = None
        if next_offset < len(rows):
            next_cursor = base64.urlsafe_b64encode(
                json.dumps({"o": next_offset, "s": scope_hash}, separators=(",", ":")).encode()
            ).decode().rstrip("=")
        return {"items": items, "next_cursor": next_cursor}

    # -- responses ---------------------------------------------------------

    def _json(self, payload: Any, req_headers: dict[str, str], snapshot: dict | None = None) -> Response:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "ETag": etag,
            "Cache-Control": "private, no-cache",
        }
        built_at = (snapshot or self._store.data or {}).get("built_at")
        if built_at:
            try:
                headers["Last-Modified"] = format_datetime(
                    datetime.strptime(built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), usegmt=True,
                )
            except ValueError:
                pass
        inm = req_headers.get("if-none-match")
        if inm and (inm.strip() == "*" or etag in [t.strip().removeprefix("W/") for t in inm.split(",")]):
            return Response(304, b"", {k: v for k, v in headers.items() if k != "Content-Type"})
        return Response(200, body, headers)

    @staticmethod
    def _error(err: ApiError) -> Response:
        body = json.dumps({"error": {"code": err.code, "message": err.message}}).encode("utf-8")
        return Response(err.status, body, {"Content-Type": "application/json; charset=utf-8", **err.headers})


def _metric_value(doc: dict, name: str) -> Any:
    for m in doc.get("metrics", []):
        if m.get("name") == name:
            return m.get("value")
    return None


def _compact(doc: dict) -> dict:
    return {
        "domain": doc["domain"],
        "first_seen_at": doc["first_seen_at"],
        "last_seen_at": doc["last_seen_at"],
        "availability": {"status": doc["availability"]["status"], "observed_at": doc["availability"]["observed_at"]},
        "metrics": doc["metrics"],
        "wayback": {k: doc["wayback"][k] for k in ("status", "snapshots", "first_year", "last_year")},
        "analysis": {"niche": None, "verdict": (doc.get("analysis") or {}).get("verdict")},
    }


# ---------------------------------------------------------------------------
# OpenAPI 3.1
# ---------------------------------------------------------------------------


def openapi_document() -> dict:
    ref = lambda name: {"$ref": f"#/components/schemas/{name}"}  # noqa: E731
    nullable = lambda t: {"type": [t, "null"]}  # noqa: E731
    error_responses = {
        "400": {"description": "bad_request", "content": {"application/json": {"schema": ref("Error")}}},
        "401": {"description": "unauthorized", "content": {"application/json": {"schema": ref("Error")}}},
        "429": {"description": "rate_limited (see Retry-After)", "content": {"application/json": {"schema": ref("Error")}}},
        "503": {"description": "snapshot not loaded yet", "content": {"application/json": {"schema": ref("Error")}}},
    }
    page_params = [
        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}},
    ]
    domain_param = {"name": "domain", "in": "path", "required": True, "schema": {"type": "string"},
                    "description": "Lowercase ASCII or punycode; Unicode input is converted."}

    def page_of(schema: dict) -> dict:
        return {"type": "object", "required": ["items", "next_cursor"], "properties": {
            "items": {"type": "array", "items": schema}, "next_cursor": nullable("string")}}

    def ok(schema: dict, description: str = "OK") -> dict:
        return {"description": description, "content": {"application/json": {"schema": schema}},
                "headers": {"ETag": {"schema": {"type": "string"}}}}

    metric = {"type": "object", "required": ["name", "value", "status", "source", "observed_at"], "properties": {
        "name": {"type": "string", "enum": ["referring_domains", "open_page_rank", "cert_history"]},
        "value": {"type": ["number", "boolean", "null"]},
        "status": {"type": "string", "enum": ["ok", "not_checked", "lookup_failed"]},
        "source": nullable("string"), "observed_at": nullable("string")}}
    wayback = {"type": "object", "properties": {
        "status": {"type": "string", "enum": ["ok", "none_found", "lookup_failed", "not_checked"]},
        "snapshots": nullable("integer"), "first_year": {"type": "null"}, "last_year": nullable("integer"),
        "sample_title": nullable("string"), "observed_at": nullable("string")}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "DomainSifter read-only API",
            "version": API_VERSION,
            "description": "Read-only. Every value carries a source; unknown is null with a status. "
                           "See the data dictionary for sources and metric names. Timestamps derived "
                           "from the archive have day precision (T00:00:00Z).",
        },
        "servers": [{"url": "https://api.domainsifter.com"}],
        "security": [{"bearer": []}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "Error": {"type": "object", "required": ["error"], "properties": {"error": {
                    "type": "object", "required": ["code", "message"], "properties": {
                        "code": {"type": "string", "enum": ["not_found", "bad_request", "unauthorized", "rate_limited", "internal"]},
                        "message": {"type": "string"}}}}},
                "Source": {"type": "object", "required": ["id", "name", "method", "terms_status", "may_display"], "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"},
                    "method": {"type": "string", "enum": ["scrape", "api", "own_crawl", "licensed_feed", "manual"]},
                    "terms_status": {"type": "string", "enum": ["permitted", "permitted_with_conditions", "prohibited", "unknown"]},
                    "may_display": {"type": "boolean"}, "collected_from": nullable("string"), "notes": {"type": "string"}}},
                "Metric": metric,
                "Wayback": wayback,
                "Analysis": {"type": ["object", "null"], "properties": {
                    "model": nullable("string"), "prompt_version": nullable("string"), "niche": {"type": "null"},
                    "score": {"type": "null"}, "spam_signal": {"type": ["string", "null"], "enum": ["low", "high", None]},
                    "verdict": {"type": ["string", "null"], "enum": ["legitimate", "parked", "empty", "toxic", None]},
                    "analyzed_at": nullable("string"),
                    "name_quality": {"type": ["object", "null"], "properties": {
                        "model": {"type": "string"}, "score": {"type": "integer"}, "analyzed_at": {"type": "string"}}}}},
                "Domain": {"type": "object", "properties": {
                    "domain": {"type": "string"}, "tld": {"type": "string"},
                    "first_seen_at": {"type": "string"}, "last_seen_at": {"type": "string"},
                    "availability": {"type": "object", "properties": {
                        "status": {"type": "string", "enum": ["dropped", "pending_delete", "available", "registered", "auction", "unknown"]},
                        "observed_at": nullable("string"), "source": nullable("string"), "drop_date": nullable("string")}},
                    "metrics": {"type": "array", "items": ref("Metric")},
                    "wayback": ref("Wayback"), "analysis": ref("Analysis"),
                    "published_on_domainsifter": {"type": "boolean"},
                    "observations_count": {"type": "integer"}}},
                "Observation": {"type": "object", "required": ["observed_at", "kind", "name", "value", "status", "source"], "properties": {
                    "observed_at": {"type": "string"},
                    "kind": {"type": "string", "enum": ["availability", "metric", "wayback", "analysis"]},
                    "name": {"type": "string"}, "value": {}, "status": {"type": "string"}, "source": {"type": "string"}}},
                "Candidate": {"type": "object", "properties": {
                    "domain": {"type": "string"}, "first_seen_at": {"type": "string"}, "last_seen_at": {"type": "string"},
                    "availability": {"type": "object"}, "metrics": {"type": "array", "items": ref("Metric")},
                    "wayback": {"type": "object"}, "analysis": {"type": "object"}}},
                "Change": {"type": "object", "properties": {
                    "domain": {"type": "string"}, "changed_at": {"type": "string"},
                    "change": {"type": "string", "enum": ["added", "updated"]}}},
            },
        },
        "paths": {
            "/v1/health": {"get": {"summary": "Freshness and status", "responses": {"200": ok({"type": "object"}), **error_responses}}},
            "/v1/sources": {"get": {"summary": "Provenance registry", "responses": {
                "200": ok({"type": "object", "properties": {"items": {"type": "array", "items": ref("Source")}}}), **error_responses}}},
            "/v1/openapi.json": {"get": {"summary": "This document", "responses": {"200": ok({"type": "object"})}}},
            "/v1/domains/{domain}": {"get": {"summary": "One domain", "parameters": [domain_param], "responses": {
                "200": ok(ref("Domain")), "404": {"description": "not in DomainSifter"}, **error_responses}}},
            "/v1/domains/{domain}/observations": {"get": {"summary": "Observation history, oldest first",
                "parameters": [domain_param, *page_params], "responses": {"200": ok(page_of(ref("Observation"))), **error_responses}}},
            "/v1/domains/{domain}/backlinks": {"get": {"summary": "Always 501: no per-link data",
                "parameters": [domain_param], "responses": {"501": {"description": "not implemented"}}}},
            "/v1/domains/{domain}/referring-domains": {"get": {"summary": "Always 501: counts only, in metrics",
                "parameters": [domain_param], "responses": {"501": {"description": "not implemented"}}}},
            "/v1/candidates": {"get": {"summary": "Filterable list", "parameters": [
                {"name": "status", "in": "query", "schema": {"type": "string"}},
                {"name": "tld", "in": "query", "schema": {"type": "string"}},
                {"name": "min_referring_domains", "in": "query", "schema": {"type": "integer", "minimum": 0}},
                {"name": "has_wayback", "in": "query", "schema": {"type": "boolean"}},
                {"name": "niche", "in": "query", "schema": {"type": "string"}, "description": "Not supported: returns 400."},
                {"name": "first_seen_after", "in": "query", "schema": {"type": "string"}},
                {"name": "updated_after", "in": "query", "schema": {"type": "string"}},
                {"name": "sort", "in": "query", "schema": {"type": "string", "enum": list(_SORTS), "default": "first_seen_desc"}},
                *page_params], "responses": {"200": ok(page_of(ref("Candidate"))), **error_responses}}},
            "/v1/changes": {"get": {"summary": "Incremental sync feed, oldest change first", "parameters": [
                {"name": "since", "in": "query", "required": True, "schema": {"type": "string"}}, *page_params],
                "responses": {"200": ok(page_of(ref("Change"))), **error_responses}}},
        },
    }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "domainsifter-api"
        sys_version = ""

        def _serve(self) -> None:
            started = time.monotonic()
            resp = api.handle(self.command, self.path, dict(self.headers.items()))
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            if self.command != "HEAD" and resp.body:
                self.wfile.write(resp.body)
            logger.info("%s %s %d %.0fms", self.command, urlsplit(self.path).path,
                        resp.status, (time.monotonic() - started) * 1000)

        do_GET = _serve
        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _serve

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib signature
            return

    return Handler


def load_tokens(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    api_cfg = config.get("api", {}) or {}
    tokens = load_tokens(os.environ.get("DOMAINSIFTER_API_TOKENS"))
    if not tokens:
        logger.error("DOMAINSIFTER_API_TOKENS is empty; refusing to start an unauthenticated API")
        return 1
    store = SnapshotStore(Path(api_cfg.get("snapshot_path", "/var/lib/domainsifter-api/snapshot.json")))
    api = Api(config, tokens, store)
    host = str(api_cfg.get("bind_host", "127.0.0.1"))
    port = int(api_cfg.get("port", 8010))
    server = ThreadingHTTPServer((host, port), _make_handler(api))
    server.daemon_threads = True
    logger.info("DomainSifter API %s listening on %s:%d (%d token(s))", API_VERSION, host, port, len(tokens))
    store.current()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
