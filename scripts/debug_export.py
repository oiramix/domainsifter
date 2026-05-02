"""Optional intermediate-list dumps for the --debug-export pipeline flag.

Off by default. When --debug-export PATH is set on the pipeline CLI, the
orchestrator collects intermediate filter/trim lists at each stage and writes
them as plain UTF-8 text files (LF endings, sorted alphabetically) to PATH so
the operator can audit what gets discarded where.

Files written:
    lexical_rejects.txt    "name,reason" — one rejection per line; reason is
                            the lexical_filter rule key (digit_ratio,
                            consonant_run, high_entropy, no_alpha_trigrams,
                            repeat_run, short_apex, too_few_matches,
                            unpronounceable, vowel_ratio).
    lexical_survivors.txt  every name that passed the lexical filter.
    trim_kept.txt          names that survived the length-asc trim cap and
                            entered the availability check.
    trim_discards.txt      names cut by the trim (passed lexical, didn't make
                            it into availability check).
    published.txt          final names written to daily-domains.json.
    _meta.json             run timestamp, per-stage counts, trim threshold.

Production runs that don't pass --debug-export never call into this module —
the pipeline gates both collection AND writing on the flag.

Atomic writes (tempfile + os.replace) mirror scripts/output.py so a crash
mid-write never leaves a partial dump on disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _atomic_write_text(target: Path, lines: list[str]) -> None:
    """Write `lines` to `target` atomically. One line per record, LF-terminated.
    File is plain UTF-8, no BOM. Empty `lines` produces an empty file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", dir=str(target.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            for line in lines:
                fh.write(line)
                fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", dir=str(target.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_dumps(
    out_dir: str | Path,
    *,
    lexical_rejects: list[tuple[str, str]],
    lexical_survivors: list[str],
    trim_kept: list[str],
    trim_discards: list[str],
    published: list[str],
    meta: dict | None = None,
) -> Path:
    """Write the five plain-text dumps (and optional _meta.json) to `out_dir`.

    All inputs are sorted alphabetically before writing so diffs and sampling
    are reproducible across runs.

    Returns the resolved out_dir Path for the caller's logging.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rej_lines = sorted(f"{name},{reason}" for name, reason in lexical_rejects)
    surv_lines = sorted(lexical_survivors)
    kept_lines = sorted(trim_kept)
    disc_lines = sorted(trim_discards)
    pub_lines = sorted(published)

    _atomic_write_text(out_dir / "lexical_rejects.txt", rej_lines)
    _atomic_write_text(out_dir / "lexical_survivors.txt", surv_lines)
    _atomic_write_text(out_dir / "trim_kept.txt", kept_lines)
    _atomic_write_text(out_dir / "trim_discards.txt", disc_lines)
    _atomic_write_text(out_dir / "published.txt", pub_lines)

    if meta is not None:
        _atomic_write_json(out_dir / "_meta.json", meta)

    logger.info(
        "Debug-export dumps written to %s "
        "(lexical_rejects=%d, lexical_survivors=%d, trim_kept=%d, trim_discards=%d, published=%d)",
        out_dir, len(rej_lines), len(surv_lines), len(kept_lines), len(disc_lines), len(pub_lines),
    )
    return out_dir
