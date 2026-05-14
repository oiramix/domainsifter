"""Startup environment validation.

Called by pipeline.py before any zone download or enrichment. Verifies that
every secret the pipeline depends on is present in the environment. If any
are missing, raises `MissingEnvVarsError` listing all of them at once (so the
operator fixes everything in one round-trip rather than playing whack-a-mole).

Required vs. optional:
    REQUIRED — pipeline aborts if missing:
        CZDS_USERNAME       — can't download zones without it
        CZDS_PASSWORD       — same
        SAFE_BROWSING_KEY   — core spam filter rule; degraded filtering is
                              worse than no run, so we fail loudly
        R2_ACCOUNT_ID       — yesterday's zone snapshots live in Cloudflare
        R2_ACCESS_KEY_ID      R2 (migrated off the repo because per-TLD
        R2_SECRET_ACCESS_KEY  state files exceeded GitHub's 100 MB limit).
        R2_BUCKET_NAME        Pipeline can't compute drops without R2 auth.
    OPTIONAL — pipeline runs with the corresponding feature skipped:
        OPENPAGERANK_KEY    — without it, no authority signal; we still
                              produce output, just with weaker scoring
        BUTTONDOWN_API_KEY  — without it, the daily newsletter step skips
                              (added 2026-05-14 alongside the newsletter
                              feature). Pipeline JSON publish is unaffected.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "CZDS_USERNAME",
    "CZDS_PASSWORD",
    "SAFE_BROWSING_KEY",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
)

OPTIONAL_ENV_VARS: tuple[str, ...] = ("OPENPAGERANK_KEY", "BUTTONDOWN_API_KEY")


class MissingEnvVarsError(RuntimeError):
    """One or more required environment variables are not set."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Set them in GitHub Secrets or your local .env before running the pipeline."
        )


def validate_env(env: dict[str, str] | None = None) -> None:
    """Raise MissingEnvVarsError if any REQUIRED_ENV_VARS are absent or empty.
    Optional vars are warned-about-but-tolerated.

    `env` defaults to os.environ; tests pass a dict for isolation."""
    source = env if env is not None else os.environ
    missing = [name for name in REQUIRED_ENV_VARS if not source.get(name)]
    if missing:
        raise MissingEnvVarsError(missing)

    for name in OPTIONAL_ENV_VARS:
        if not source.get(name):
            logger.warning(
                "Optional env var %s not set; the corresponding enrichment will be skipped.",
                name,
            )
        else:
            logger.info("Optional env var %s is set.", name)

    logger.info("All required environment variables are present.")
