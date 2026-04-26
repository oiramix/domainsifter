"""Streaming zone-file parser.

Reads a gzipped DNS zone file line by line, extracts the apex domain from each
record (column 1), and yields a deduplicated set of lowercase names with no
trailing dot.

CZDS zone files are standard RFC 1035 master files with TLD-rooted FQDNs:
    example.com.  3600  IN  NS  ns1.registrar.com.
    example.com.  3600  IN  DS  12345 8 2 ABCD...
    example.com.  3600  IN  RRSIG NS ...

Many records per domain — a single apex appears across NS, DS, RRSIG, A, AAAA.
We only need the apex name, so we dedupe via a set as we stream.

Lines we skip:
- Blank lines
- Comments starting with `;`
- Directives starting with `$` ($ORIGIN, $TTL)
- Lines whose owner name is empty after stripping (continuation records — rare
  in CZDS but cheap to guard against)

We do NOT decompress to disk. Input is a path to a .gz file; we stream-decompress
through gzip.open in text mode.
"""

from __future__ import annotations

import gzip
import logging
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


def iter_apex_names(zone_path: str | Path) -> Iterator[str]:
    """Yield each owner-name token (column 1) found in the zone, lowercased
    with any trailing dot removed. May yield duplicates; caller dedupes if
    they want a set. Use `parse_zone` for the deduped variant.
    """
    with gzip.open(zone_path, "rt", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            if line[0] in (";", "$"):
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            owner = parts[0].rstrip(".").lower()
            if not owner:
                continue
            yield owner


def parse_zone(zone_path: str | Path) -> set[str]:
    """Return the set of unique apex names found in the zone file."""
    domains: set[str] = set()
    for name in iter_apex_names(zone_path):
        domains.add(name)
    logger.info("Parsed %d unique apex names from %s", len(domains), zone_path)
    return domains
