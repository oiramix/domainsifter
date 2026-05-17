"""IPv6 /128 rotation diagnostic against RDAP registries.

Reconnaissance script — NOT part of the daily pipeline, NOT imported by any
production module. Reuses the registry endpoint list from scripts/config.json
(api_min_interval_seconds.rdap_per_host) but does nothing else with the
config and writes nothing to disk.

The question this answers: when we route each RDAP request from a different
random /128 within our OVH /64 (vs. all from one fixed /128), do per-IP
rate limits become less aggressive? If yes, IPv6 rotation could let us
tighten our pipeline-side throttles without ban risk.

Output is a per-registry table on stdout plus optional --json. Phase 2
(actual production rotation) is a downstream decision based on this report.

PREREQUISITES (server-side, NOT validated by this script):
  - sysctl net.ipv6.ip_nonlocal_bind = 1
  - ip -6 route add local <YOUR_/64>/64 dev lo  (some kernels need this)
  - Smoke test before running:
        curl -s --interface <random /128 in /64> https://rdap.verisign.com/v1/domain/example.net

SAFETY (enforced by the script):
  - Refuses to run outside 14:00-20:00 UTC unless --force-window is passed.
    Production cron runs 06:30-08:30 UTC; this window leaves multi-hour
    separation on both sides so registry-side rate trackers don't conflate
    cron traffic with diagnostic traffic.
  - 200ms minimum gap between requests per registry (5 req/sec ceiling).
    --gap-seconds rejects values below the floor.
  - Stops a registry phase immediately on 5 consecutive 429s. The streak
    counter resets on any non-429 response.
  - 10 hardcoded test domains per registry (TEST_DOMAINS dict below). Does
    NOT touch src/data/daily-domains.json — those have already been hit
    by the cron and would bias the results.

BUDGET RECONCILIATION:
The spec describes "10 domains × 5 requests × 2 phases = 100 per registry"
in the per-registry loop and ALSO "Total request budget: max 350 (50 per
registry max)". Those two are inconsistent at >=4 registries. This script
defaults to the per-loop description (REQUESTS_PER_DOMAIN=5 → 50 per phase
per registry → 100 per registry → ~800 across 8 registries). To fit the
350-total / 50-per-registry cap, pass --requests-per-domain 2 (40 per
registry, ~320 total across 8 registries).

OUTPUT-ONLY: prints findings, does not modify pipeline behavior or config.
Phase 2 decisions are made externally based on this report.

Usage:
    python -m scripts.diagnostic_ipv6_rdap [--config scripts/config.json]
        [--prefix 2001:41d0:abcd:ef00] [--requests-per-domain N]
        [--gap-seconds 0.2] [--force-window] [--json] [--verbose]

Exit codes:
    0 — diagnostic completed (report printed)
    1 — refused to run (window, sysctl, missing config) or preflight failed
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import random
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("scripts.diagnostic_ipv6_rdap")


# --- Constants ---------------------------------------------------------------

DEFAULT_REQUESTS_PER_DOMAIN = 5
DEFAULT_GAP_SECONDS = 0.2
CONSECUTIVE_429_ABORT = 5
REQUEST_TIMEOUT_SECONDS = 15
HEADER_READ_LIMIT_BYTES = 8192
WINDOW_START_HOUR_UTC = 14
WINDOW_END_HOUR_UTC = 20

# Verdict thresholds (percentages, not fractions).
ROTATION_GAIN_THRESHOLD_PCT = 20.0
TRACKING_TOLERANCE_PCT = 10.0
INCONCLUSIVE_FAIL_THRESHOLD_PCT = 60.0


# --- Test domain fixtures ---------------------------------------------------
#
# 10 stable real domains per registry. Picked for "this name is almost
# certainly registered and elicits a real RDAP response." The actual
# response code (200 / 404) doesn't matter — both prove the registry
# processed the request (not rate-limited). Only 429 counts as failure.
#
# Domains are NOT pulled from daily-domains.json so they don't overlap with
# the production cron's footprint (which would bias the test).

TEST_DOMAINS: dict[str, list[str]] = {
    "rdap.verisign.com": [
        "google.com", "microsoft.com", "amazon.com", "apple.com",
        "facebook.com", "github.com", "linkedin.com", "netflix.com",
        "cloudflare.com", "nytimes.com",
    ],
    "rdap.publicinterestregistry.org": [
        "wikipedia.org", "mozilla.org", "kernel.org", "gnu.org", "w3.org",
        "ietf.org", "isoc.org", "archlinux.org", "debian.org", "fsf.org",
    ],
    "rdap.identitydigital.services": [
        # Identity Digital serves .live, .studio among others.
        "google.live", "microsoft.live", "outlook.live", "skype.live",
        "google.studio", "youtube.studio", "amazon.studio",
        "fan.studio", "tax.studio", "shop.studio",
    ],
    "rdap.gmoregistry.net": [
        # GMO primarily serves .shop.
        "google.shop", "amazon.shop", "nike.shop", "samsung.shop",
        "rolex.shop", "ebay.shop", "etsy.shop", "casio.shop",
        "puma.shop", "panda.shop",
    ],
    "rdap.nic.biz": [
        "ibm.biz", "google.biz", "microsoft.biz", "amazon.biz",
        "twitter.biz", "facebook.biz", "apple.biz", "cisco.biz",
        "oracle.biz", "fox.biz",
    ],
    "rdap.centralnic.com": [
        # CentralNic-managed: .site, .online, .store, .xyz (partial).
        "google.site", "facebook.site", "microsoft.site",
        "amazon.online", "google.online", "microsoft.online",
        "amazon.store", "google.store",
        "x.xyz", "ai.xyz",
    ],
    "rdap.radix.host": [
        # Radix: .tech, .store, .website, .online (partial).
        "x.tech", "ai.tech", "info.tech", "data.tech", "edu.tech",
        "ai.store", "best.store", "online.store",
        "google.website", "ai.website",
    ],
    "pubapi.registry.google": [
        # Google Registry: .app, .dev.
        "google.app", "youtube.app", "play.app", "search.app",
        "google.dev", "web.dev", "flutter.dev", "angular.dev",
        "chrome.dev", "android.dev",
    ],
}


# --- IPv6 helpers ------------------------------------------------------------


def detect_local_ipv6_prefix() -> str | None:
    """Best-effort detection of the assigned /64. Returns 4 hextets (the
    prefix) as a string like '2001:41d0:abcd:ef00', or None on failure.
    Caller should accept --prefix as a CLI override when detection fails.

    Linux-only: parses `ip -6 -o addr show scope global`. On non-Linux this
    returns None and the user must pass --prefix manually.
    """
    try:
        out = subprocess.check_output(
            ["ip", "-6", "-o", "addr", "show", "scope", "global"],
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).decode("ascii", errors="replace")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        m = re.search(r"inet6\s+([0-9a-fA-F:]+)/(\d+)", line)
        if not m:
            continue
        addr, prefixlen = m.group(1), int(m.group(2))
        if prefixlen != 64:
            continue
        try:
            full = ipaddress.IPv6Address(addr).exploded
            return ":".join(full.split(":")[:4])
        except ipaddress.AddressValueError:
            continue
    return None


def random_v6_in_prefix(prefix4: str, rng: random.Random | None = None) -> str:
    """Generate a random /128 in the /64 described by `prefix4` (4 hextets).
    rng is injectable for deterministic tests."""
    r = rng or random
    suffix = ":".join(f"{r.randint(0, 0xffff):x}" for _ in range(4))
    return f"{prefix4}:{suffix}"


def redact_v6(addr: str) -> str:
    """Log-safe form: keep only the /64 prefix, mask the suffix. The /64 is
    public infrastructure (OVH-assigned, shows up in WHOIS); the suffix
    pin-points one particular diagnostic request and isn't worth leaving in
    journalctl forever."""
    try:
        full = ipaddress.IPv6Address(addr).exploded
    except (ipaddress.AddressValueError, ValueError):
        return "***invalid-v6***"
    return ":".join(full.split(":")[:4]) + ":***"


def normalize_prefix(prefix: str) -> str:
    """Accept either '2001:41d0:abcd:ef00' (4 hextets) or a /64 CIDR like
    '2001:41d0:abcd:ef00::/64'. Return the 4-hextet form."""
    prefix = prefix.strip()
    if "/" in prefix:
        net = ipaddress.IPv6Network(prefix, strict=False)
        if net.prefixlen != 64:
            raise ValueError(f"Expected a /64 prefix, got /{net.prefixlen}")
        return ":".join(net.network_address.exploded.split(":")[:4])
    # 4-hextet input — validate it expands to a real /64 boundary.
    parts = prefix.split(":")
    if len(parts) != 4 or not all(0 < len(p) <= 4 for p in parts):
        raise ValueError(
            f"Expected 4 colon-separated hextets, got {prefix!r}"
        )
    for p in parts:
        int(p, 16)  # raises ValueError on non-hex
    return prefix


# --- HTTP / RDAP request -----------------------------------------------------


def _parse_status_and_retry_after(raw_header: bytes) -> tuple[int | None, str | None]:
    """Pull the HTTP status code and (if 429) the Retry-After header value
    from the response header block."""
    try:
        text = raw_header.decode("ascii", errors="replace")
    except UnicodeDecodeError:
        return None, None
    lines = text.split("\r\n")
    if not lines:
        return None, None
    status: int | None = None
    first_toks = lines[0].split(" ", 2)
    if len(first_toks) >= 2 and first_toks[1].isdigit():
        status = int(first_toks[1])
    retry_after: str | None = None
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        if name.strip().lower() == "retry-after":
            retry_after = value.strip()
            break
    return status, retry_after


def rdap_get(
    host: str,
    domain: str,
    source_ip: str,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Issue a single RDAP request bound to `source_ip`. Returns a dict with
    keys: status (int|None), elapsed_ms (float), error (str|None),
    retry_after (str|None). Status is None on transport failure."""
    path = f"/v1/domain/{domain}"
    start = time.monotonic()
    raw_sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    try:
        addrs = socket.getaddrinfo(
            host, 443, family=socket.AF_INET6, type=socket.SOCK_STREAM,
        )
        if not addrs:
            return {
                "status": None,
                "elapsed_ms": (time.monotonic() - start) * 1000.0,
                "error": "no_aaaa",
                "retry_after": None,
            }
        family, socktype, proto, _, sockaddr = addrs[0]
        raw_sock = socket.socket(family, socktype, proto)
        raw_sock.settimeout(timeout)
        raw_sock.bind((source_ip, 0))
        raw_sock.connect(sockaddr)
        ctx = ssl.create_default_context()
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: domainsifter-diag/1.0\r\n"
            f"Accept: application/rdap+json\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        tls_sock.sendall(req.encode("ascii"))
        # Read just enough to capture status + headers, not the body. RDAP
        # bodies for unknown domains are tiny anyway, but bounding the read
        # keeps the wall clock predictable.
        buf = bytearray()
        while b"\r\n\r\n" not in buf and len(buf) < HEADER_READ_LIMIT_BYTES:
            chunk = tls_sock.recv(1024)
            if not chunk:
                break
            buf.extend(chunk)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        header_end = buf.find(b"\r\n\r\n")
        header_block = bytes(buf[:header_end]) if header_end >= 0 else bytes(buf)
        status, retry_after = _parse_status_and_retry_after(header_block)
        return {
            "status": status,
            "elapsed_ms": elapsed_ms,
            "error": None if status is not None else "bad_response",
            "retry_after": retry_after,
        }
    except socket.timeout:
        return {
            "status": None,
            "elapsed_ms": (time.monotonic() - start) * 1000.0,
            "error": "timeout",
            "retry_after": None,
        }
    except OSError as exc:
        # EADDRNOTAVAIL surfaces here when ip_nonlocal_bind isn't set or the
        # /64 isn't routed. Caller's preflight should have caught it.
        return {
            "status": None,
            "elapsed_ms": (time.monotonic() - start) * 1000.0,
            "error": type(exc).__name__,
            "retry_after": None,
        }
    except ssl.SSLError as exc:
        return {
            "status": None,
            "elapsed_ms": (time.monotonic() - start) * 1000.0,
            "error": f"ssl:{type(exc).__name__}",
            "retry_after": None,
        }
    finally:
        for s in (tls_sock, raw_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


# --- Phase runners -----------------------------------------------------------


def run_phase(
    *,
    host: str,
    domains: list[str],
    prefix4: str,
    rotation: bool,
    requests_per_domain: int,
    gap_seconds: float,
    single_ip: str | None = None,
    rng: random.Random | None = None,
) -> dict:
    """Run one phase against a registry. Returns aggregated counts.

    `rotation=True`: source /128 randomized per request from prefix4.
    `rotation=False`: source /128 fixed to `single_ip` (required).
    """
    if not rotation and single_ip is None:
        raise ValueError("single_ip required for non-rotation phase")
    counts: dict[str, Any] = {
        "requests": 0,
        "by_status": {},
        "elapsed_ms": [],
        "retry_after_samples": [],
        "aborted": False,
    }
    consecutive_429 = 0
    for domain in domains:
        for _ in range(requests_per_domain):
            source = (
                random_v6_in_prefix(prefix4, rng=rng) if rotation else single_ip
            )
            result = rdap_get(host, domain, source)  # type: ignore[arg-type]
            counts["requests"] += 1
            key = (
                str(result["status"]) if result["status"] is not None
                else f"err:{result['error']}"
            )
            counts["by_status"][key] = counts["by_status"].get(key, 0) + 1
            counts["elapsed_ms"].append(result["elapsed_ms"])
            if result["status"] == 429 and result["retry_after"]:
                counts["retry_after_samples"].append(result["retry_after"])
            logger.info(
                "host=%s phase=%s domain=%s src=%s status=%s elapsed=%.0fms%s",
                host,
                "ROT" if rotation else "SGL",
                domain,
                redact_v6(source),  # type: ignore[arg-type]
                key,
                result["elapsed_ms"],
                f" retry-after={result['retry_after']}" if result["retry_after"] else "",
            )
            if result["status"] == 429:
                consecutive_429 += 1
                if consecutive_429 >= CONSECUTIVE_429_ABORT:
                    logger.warning(
                        "[%s] %s phase aborted after %d consecutive 429s",
                        host, "rotation" if rotation else "single", consecutive_429,
                    )
                    counts["aborted"] = True
                    return counts
            else:
                consecutive_429 = 0
            time.sleep(gap_seconds)
    return counts


# --- Verdict computation -----------------------------------------------------


def success_rate(counts: dict) -> float:
    """% of requests that returned 2xx/3xx OR 404. 404 is a valid registry
    response (domain not in registry) — it proves the registry processed
    our request and wasn't throttling. Errors and 429 count as failure."""
    if counts["requests"] == 0:
        return 0.0
    successes = 0
    for k, n in counts["by_status"].items():
        if k.startswith("err:"):
            continue
        if k == "404":
            successes += n
            continue
        try:
            code = int(k)
        except ValueError:
            continue
        if 200 <= code < 400:
            successes += n
    return 100.0 * successes / counts["requests"]


def count_status(counts: dict, status: str) -> int:
    return counts["by_status"].get(status, 0)


def verdict(rot_counts: dict, sgl_counts: dict) -> str:
    """Apply the heuristic from the spec to label one registry."""
    rot = success_rate(rot_counts)
    sgl = success_rate(sgl_counts)
    rot_fail = 100.0 - rot
    sgl_fail = 100.0 - sgl
    delta = rot - sgl

    # Inconclusive: both failing heavily — registry under stress or hitting
    # a non-IP-keyed limit (e.g. /domain rate-keyed).
    if rot_fail > INCONCLUSIVE_FAIL_THRESHOLD_PCT and sgl_fail > INCONCLUSIVE_FAIL_THRESHOLD_PCT:
        return "Inconclusive (registry under stress or different limit)"

    if delta >= ROTATION_GAIN_THRESHOLD_PCT:
        return "Rotation effective at /128"
    if abs(delta) <= TRACKING_TOLERANCE_PCT:
        return "No rotation benefit (tracking at /64 or higher)"
    return f"Marginal (rotation gain {delta:+.0f}pts, under 20pt threshold)"


# --- Preflight ---------------------------------------------------------------


def _in_window(now: datetime | None = None) -> bool:
    n = now or datetime.now(timezone.utc)
    return WINDOW_START_HOUR_UTC <= n.hour < WINDOW_END_HOUR_UTC


def preflight_bind(prefix4: str) -> tuple[bool, str | None]:
    """Try to create an AF_INET6 socket and bind to a random /128 in the
    prefix. Fails loudly if the kernel rejects the bind (typically
    EADDRNOTAVAIL, meaning ip_nonlocal_bind=0 or the /64 isn't routed)."""
    addr = random_v6_in_prefix(prefix4)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.bind((addr, 0))
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


# --- Report rendering --------------------------------------------------------


def render_table(findings: list[dict]) -> str:
    headers = [
        "Registry", "Rot 2xx/4xx", "Sgl 2xx/4xx",
        "Rot 429s", "Sgl 429s", "Verdict",
    ]
    rows: list[list[str]] = [headers]
    for f in findings:
        rot_succ = sum(
            n for k, n in f["rotation_counts"]["by_status"].items()
            if k == "200" or k == "404" or k.startswith("2") or k.startswith("3")
        )
        sgl_succ = sum(
            n for k, n in f["single_counts"]["by_status"].items()
            if k == "200" or k == "404" or k.startswith("2") or k.startswith("3")
        )
        rows.append([
            f["host"],
            f"{rot_succ}/{f['rotation_counts']['requests']}",
            f"{sgl_succ}/{f['single_counts']['requests']}",
            str(count_status(f["rotation_counts"], "429")),
            str(count_status(f["single_counts"], "429")),
            f["verdict"],
        ])
    widths = [max(len(r[c]) for r in rows) for c in range(len(headers))]
    out_lines: list[str] = []
    for i, r in enumerate(rows):
        line = " | ".join(c.ljust(widths[k]) for k, c in enumerate(r))
        out_lines.append(line)
        if i == 0:
            out_lines.append("-" * len(line))
    return "\n".join(out_lines)


def render_notes(findings: list[dict]) -> str:
    lines: list[str] = []
    for f in findings:
        host = f["host"]
        # Unusual status codes (anything that isn't 200/404/429).
        all_codes: set[str] = set()
        for counts_name in ("rotation_counts", "single_counts"):
            all_codes.update(f[counts_name]["by_status"].keys())
        unusual = sorted(
            c for c in all_codes
            if c not in {"200", "404", "429"} and not c.startswith("err:")
        )
        if unusual:
            lines.append(f"  {host}: unusual HTTP codes seen: {unusual}")
        # Transport errors.
        err_codes = sorted(c for c in all_codes if c.startswith("err:"))
        if err_codes:
            lines.append(f"  {host}: transport errors: {err_codes}")
        if f["rotation_counts"]["aborted"]:
            lines.append(f"  {host}: ROTATION phase aborted on 429 streak.")
        if f["single_counts"]["aborted"]:
            lines.append(f"  {host}: SINGLE-IP phase aborted on 429 streak.")
        # Median elapsed time per phase — a coarse signal of registry stress.
        for phase, key in (("rotation", "rotation_counts"), ("single-IP", "single_counts")):
            times = sorted(f[key]["elapsed_ms"])
            if times:
                med = times[len(times) // 2]
                lines.append(f"  {host}: median elapsed ({phase}) = {med:.0f}ms")
        # Retry-After samples — registry's own guidance.
        for phase, key in (("rotation", "rotation_counts"), ("single-IP", "single_counts")):
            samples = f[key]["retry_after_samples"]
            if samples:
                lines.append(
                    f"  {host}: Retry-After values ({phase}) = "
                    f"{sorted(set(samples))[:5]}"
                )
    return "\n".join(lines) if lines else "  (no anomalies)"


# --- CLI / main --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Empirical diagnostic: does IPv6 /128 rotation defeat per-IP "
            "RDAP rate limits? Output-only, no pipeline side effects."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.json"),
        help="Path to scripts/config.json (default next to this module).",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help=(
            "IPv6 /64 as either 4 hextets ('2001:41d0:abcd:ef00') or CIDR "
            "('2001:41d0:abcd:ef00::/64'). Auto-detected from `ip -6 addr "
            "show` when omitted; required on non-Linux."
        ),
    )
    parser.add_argument(
        "--requests-per-domain",
        type=int,
        default=DEFAULT_REQUESTS_PER_DOMAIN,
        help=(
            "Per-domain request count for each of the two phases. Default 5 "
            "(= 50/phase = 100/registry over 10 domains). Use 2 to stay "
            "under the 50-per-registry stated budget cap."
        ),
    )
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=DEFAULT_GAP_SECONDS,
        help=f"Inter-request gap. Floor is {DEFAULT_GAP_SECONDS}s.",
    )
    parser.add_argument(
        "--force-window",
        action="store_true",
        help=(
            "Skip the 14-20 UTC safety check. Use only when you have "
            "deliberate context (e.g. testing on a Saturday with cron paused)."
        ),
    )
    parser.add_argument("--json", action="store_true",
                        help="Also print findings as JSON after the table.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if not _in_window() and not args.force_window:
        now = datetime.now(timezone.utc)
        logger.error(
            "Outside the 14-20 UTC safety window (now=%s UTC). "
            "Pass --force-window to override.",
            now.strftime("%H:%M:%SZ"),
        )
        return 1

    if args.gap_seconds < DEFAULT_GAP_SECONDS:
        logger.error(
            "--gap-seconds=%.3f below the safety floor of %.3fs.",
            args.gap_seconds, DEFAULT_GAP_SECONDS,
        )
        return 1

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logger.error("Config file not found: %s", cfg_path)
        return 1
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Config unreadable: %s", exc)
        return 1

    if args.prefix:
        try:
            prefix4 = normalize_prefix(args.prefix)
        except ValueError as exc:
            logger.error("Invalid --prefix: %s", exc)
            return 1
    else:
        prefix4 = detect_local_ipv6_prefix()
        if not prefix4:
            logger.error(
                "No global IPv6 /64 detected. Pass --prefix 2001:41d0:abcd:ef00 "
                "(4 hextets) or --prefix 2001:41d0:abcd:ef00::/64 (CIDR).",
            )
            return 1
    logger.info("Using /64 prefix: %s::/64", prefix4)

    # Preflight: confirm we can bind to a random /128 in the prefix. Catches
    # the ip_nonlocal_bind=0 case before burning request budget.
    ok, err = preflight_bind(prefix4)
    if not ok:
        logger.error(
            "Cannot bind to a random /128 in %s::/64 — %s. "
            "Run: sysctl -w net.ipv6.ip_nonlocal_bind=1  (persist in /etc/sysctl.conf) "
            "and 'ip -6 route add local %s::/64 dev lo' if needed.",
            prefix4, err, prefix4,
        )
        return 1

    # Deterministic single-IP control: ::1 within the prefix so reruns of
    # the control phase are comparable across days.
    single_ip = f"{prefix4}:0:0:0:1"

    rdap_per_host = (
        config.get("api_min_interval_seconds", {}).get("rdap_per_host", {}) or {}
    )
    hosts = [h for h in rdap_per_host.keys() if not h.startswith("_")]
    tested_hosts = [h for h in hosts if h in TEST_DOMAINS]
    skipped = [h for h in hosts if h not in TEST_DOMAINS]
    for h in skipped:
        logger.warning("Skipping %s — no TEST_DOMAINS fixture defined.", h)
    if not tested_hosts:
        logger.error("No registry hosts to test (config lists none we have fixtures for).")
        return 1

    findings: list[dict] = []
    for host in tested_hosts:
        domains = TEST_DOMAINS[host]
        logger.info(
            "===== Registry: %s — %d domains × %d req/domain × 2 phases =====",
            host, len(domains), args.requests_per_domain,
        )
        rot_counts = run_phase(
            host=host, domains=domains, prefix4=prefix4, rotation=True,
            requests_per_domain=args.requests_per_domain,
            gap_seconds=args.gap_seconds,
        )
        sgl_counts = run_phase(
            host=host, domains=domains, prefix4=prefix4, rotation=False,
            requests_per_domain=args.requests_per_domain,
            gap_seconds=args.gap_seconds, single_ip=single_ip,
        )
        findings.append({
            "host": host,
            "rotation_counts": rot_counts,
            "single_counts": sgl_counts,
            "rotation_success_pct": success_rate(rot_counts),
            "single_success_pct": success_rate(sgl_counts),
            "verdict": verdict(rot_counts, sgl_counts),
        })

    print()
    print("=" * 100)
    print("IPv6 /128-rotation diagnostic — per-registry verdict")
    print("=" * 100)
    print(render_table(findings))
    print()
    print("Notes:")
    print(render_notes(findings))

    if args.json:
        print()
        compact: list[dict] = []
        for f in findings:
            ff = dict(f)
            for key in ("rotation_counts", "single_counts"):
                inner = dict(f[key])
                times = sorted(inner.pop("elapsed_ms", []))
                inner["median_elapsed_ms"] = (
                    times[len(times) // 2] if times else None
                )
                ff[key] = inner
            compact.append(ff)
        print(json.dumps(compact, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
