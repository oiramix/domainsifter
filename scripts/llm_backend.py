"""Uniform LLM backend for pipeline stages that need model inference.

WHY THIS EXISTS
---------------
The Anthropic API credit balance hit zero on 2026-07-23. Both LLM stages
(`phase2_ranker`, `snapshot_classifier`) have been failing soft ever since:
the ranker reverts to mechanical selection, the classifier returns `unknown`
for every domain, and the toxic-domain gate in `filter.keep_post_enrichment`
never fires. DomainSifter has no revenue, so credits are not being refunded.

This module lets those stages run on Mario's Max subscription instead, by
shelling out to Claude Code (`claude -p`) rather than calling the metered
API. Both paths are kept behind one interface so the switch is a config
change (hard rule 9) and the rollback is one line.

BACKENDS
--------
- ``api``          — the metered Anthropic API (the historical path; needs
                     ANTHROPIC_API_KEY and a funded balance).
- ``claude_code``  — subprocess to the ``claude`` CLI, authenticated with a
                     subscription OAuth token. No per-token cost.

CALIBRATION (measured 2026-09-17 on the OVH box, 200 names, real prompt)
-----------------------------------------------------------------------
- 29.4 output tokens per ranked name  → ~1,090 names per 32k-output call;
  we chunk at 800 for margin.
- ~115 output tokens/sec              → a 800-name chunk takes ~3.5 min.
- ~25k tokens of Claude Code system prompt + tool defs ride along on every
  call, cached across calls within the hour. This is why callers MUST batch
  aggressively: one call per domain would be almost all overhead.
- Extended thinking defaults ON and is pure waste here (647 thinking tokens
  on a 3-name call). ``MAX_THINKING_TOKENS=0`` disables it and is set below.

TWO TRAPS THIS MODULE EXISTS TO AVOID
-------------------------------------
1. ``ANTHROPIC_API_KEY`` outranks ``CLAUDE_CODE_OAUTH_TOKEN`` in Claude
   Code's documented auth precedence. The pipeline's own .env still defines
   it, so the child environment scrubs it — otherwise every call would try
   to bill the empty API account and fail.
2. Claude Code is a conversational agent, not a raw completion endpoint. A
   loosely-worded prompt gets a refusal or a prose preamble instead of JSON
   (observed: asking to score "registrability" was declined as a
   trademark-law question). `parse_json_array` therefore treats prose as a
   hard failure so the caller trips its existing fallback rather than
   silently recording an empty result.

The token itself is passed via the child's environment, never argv, so it
cannot leak into `ps` output or journalctl.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Protocol

logger = logging.getLogger("scripts.llm_backend")


class LLMBackendError(RuntimeError):
    """Any backend failure: transport, auth, timeout, refusal, bad shape.

    Callers are expected to catch this and fall back, never to crash the
    pipeline (hard rule 17: a single API source being down is not fatal).
    """


# Defaults applied when a key is absent from config["llm"]. Every value is
# overridable from scripts/config.json per hard rule 9.
DEFAULTS: dict[str, Any] = {
    "backend": "claude_code",
    "binary": "/usr/bin/claude",
    "model": "haiku",
    "timeout_seconds": 900,
    "env_file": "/etc/domainsifter/claude.env",
    "max_parallel": 3,
    "api_model": "claude-haiku-4-5-20251001",
    "api_max_tokens": 8192,
}


def cfg(config: dict, key: str) -> Any:
    """Read ``config["llm"][key]`` falling back to DEFAULTS."""
    return dict(config or {}).get("llm", {}).get(key, DEFAULTS[key])


class Backend(Protocol):
    """One text-in/text-out call. Implementations never raise anything but
    LLMBackendError."""

    name: str

    def complete(
        self, *, system: str, user: str, timeout_seconds: int | None = None
    ) -> str:
        """Return the assistant's raw text reply."""
        ...


def _read_env_file(path: str) -> dict[str, str]:
    """Parse a systemd-style KEY=VALUE file. Missing file yields {}.

    Values are taken literally: no quote stripping, no interpolation, which
    matches systemd's EnvironmentFile= semantics and avoids a class of bug
    where a quoted token authenticates locally but not under systemd.
    """
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key:
                    # A malformed `=value` line would otherwise inject an
                    # empty-string key into the child environment.
                    continue
                out[key] = value
    except OSError:
        logger.debug("llm_backend: no env file at %s", path)
    return out


class ClaudeCodeBackend:
    """Shells out to the `claude` CLI, authenticated by subscription."""

    name = "claude_code"

    def __init__(self, config: dict) -> None:
        self._binary = str(cfg(config, "binary"))
        self._model = str(cfg(config, "model"))
        self._timeout = int(cfg(config, "timeout_seconds"))
        self._env_file = str(cfg(config, "env_file"))

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(_read_env_file(self._env_file))
        # Trap 1: the API key outranks the OAuth token. Scrub it.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        # Thinking is pure cost for structured extraction work.
        env["MAX_THINKING_TOKENS"] = "0"
        # The service account has /usr/sbin/nologin; HOME may be unset under
        # systemd, and Claude Code needs a writable config dir.
        env.setdefault("HOME", "/home/domainsifter")
        return env

    def complete(
        self, *, system: str, user: str, timeout_seconds: int | None = None
    ) -> str:
        timeout = int(timeout_seconds or self._timeout)
        argv = [
            self._binary,
            "-p",
            user,
            "--model",
            self._model,
            "--output-format",
            "json",
            "--append-system-prompt",
            system,
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMBackendError(f"claude -p timed out after {timeout}s") from exc
        except OSError as exc:
            raise LLMBackendError(f"cannot execute {self._binary}: {exc}") from exc

        if proc.returncode != 0:
            raise LLMBackendError(
                f"claude -p exited {proc.returncode}: {(proc.stderr or '')[:300]}"
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMBackendError(
                f"claude -p emitted non-JSON envelope: {(proc.stdout or '')[:200]}"
            ) from exc

        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise LLMBackendError(
                f"claude -p reported failure: subtype={envelope.get('subtype')} "
                f"status={envelope.get('api_error_status')}"
            )
        result = envelope.get("result")
        if not isinstance(result, str) or not result.strip():
            raise LLMBackendError("claude -p returned an empty result")

        usage = envelope.get("usage") or {}
        logger.info(
            "llm_backend[claude_code]: ok — out=%s tokens, cache_read=%s, %sms",
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            envelope.get("duration_api_ms"),
        )
        return result


class ApiBackend:
    """The historical metered-API path. Retained for one-line rollback."""

    name = "api"

    def __init__(self, config: dict) -> None:
        self._model = str(cfg(config, "api_model"))
        self._max_tokens = int(cfg(config, "api_max_tokens"))
        self._timeout = int(cfg(config, "timeout_seconds"))

    def complete(
        self, *, system: str, user: str, timeout_seconds: int | None = None
    ) -> str:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency present in prod
            raise LLMBackendError("anthropic SDK not installed") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMBackendError("ANTHROPIC_API_KEY is not set")
        try:
            client = anthropic.Anthropic(
                timeout=float(timeout_seconds or self._timeout)
            )
            message = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # SDK raises a wide family; all are non-fatal
            raise LLMBackendError(f"anthropic API call failed: {exc}") from exc
        parts = [b.text for b in getattr(message, "content", []) if hasattr(b, "text")]
        text = "".join(parts).strip()
        if not text:
            raise LLMBackendError("anthropic API returned an empty message")
        # Mirrors the claude_code line so send_report can identify the backend
        # on either path; without it a rollback to `api` reports "(none used)".
        usage = getattr(message, "usage", None)
        logger.info(
            "llm_backend[api]: ok — out=%s tokens, in=%s",
            getattr(usage, "output_tokens", None),
            getattr(usage, "input_tokens", None),
        )
        return text


def get_backend(config: dict) -> Backend:
    """Build the backend named by ``config["llm"]["backend"]``.

    Raises LLMBackendError for an unknown name so a config typo surfaces at
    the call site as an ordinary fallback rather than a crash.
    """
    name = str(cfg(config, "backend")).strip().lower()
    if name == "claude_code":
        return ClaudeCodeBackend(config)
    if name == "api":
        return ApiBackend(config)
    raise LLMBackendError(f"unknown llm.backend {name!r} (want 'api' or 'claude_code')")


def _strip_fences(text: str) -> str:
    """Remove a leading ```json / ``` fence and its trailing counterpart."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    newline = stripped.find("\n")
    if newline == -1:
        return stripped
    body = stripped[newline + 1 :]
    closing = body.rfind("```")
    return (body[:closing] if closing != -1 else body).strip()


def _salvage_objects(body: str) -> list[dict]:
    """Pull every well-formed JSON object out of a malformed array.

    Why this exists: on 2026-09-19, the first production run lost two whole
    chunks — 1,600 scored names — to `Expecting ',' delimiter`. A single bad
    character somewhere in a 24,000-token reply discarded 800 otherwise
    perfect scores, because a strict parse is all-or-nothing.

    Scans for balanced `{...}` spans while respecting string literals and
    escapes, so a brace inside a "reason" value cannot desync the depth
    count. Each span is parsed independently; bad ones are skipped.

    This does NOT weaken refusal detection (trap 2 in the module docstring):
    prose contains no JSON objects, so salvage yields nothing and the caller
    still gets an LLMBackendError.
    """
    out: list[dict] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for i, ch in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(body[start : i + 1])
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            out.append(obj)
                    start = None
    return out


def parse_json_array(text: str) -> list[dict]:
    """Parse the model's reply as a JSON array of objects.

    Tolerates a markdown fence and surrounding chatter by slicing to the
    outermost brackets. Raises LLMBackendError when the reply is prose, an
    object, or otherwise not a list of dicts — see trap 2 in the module
    docstring: a refusal must trip the caller's fallback, never be mistaken
    for "no results".
    """
    body = _strip_fences(text)
    start, end = body.find("["), body.rfind("]")
    if start == -1:
        raise LLMBackendError(f"reply is not a JSON array (prose?): {body[:200]!r}")
    if end <= start:
        # An opening bracket but no closing one: the model was cut off
        # mid-array. Four chunks did exactly this on 2026-09-19. Everything
        # it managed to emit is still usable.
        salvaged = _salvage_objects(body[start:])
        if salvaged:
            logger.warning(
                "llm_backend: reply was truncated mid-array; salvaged %d "
                "objects from it rather than discarding the batch",
                len(salvaged),
            )
            return salvaged
        raise LLMBackendError(f"reply is not a JSON array (prose?): {body[:200]!r}")
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        # Best-effort recovery before giving up on the whole batch — see
        # _salvage_objects for why losing 800 names to one stray comma is
        # not acceptable.
        salvaged = _salvage_objects(body[start : end + 1])
        if salvaged:
            logger.warning(
                "llm_backend: reply was malformed JSON (%s); salvaged %d "
                "objects from it rather than discarding the batch",
                exc, len(salvaged),
            )
            return salvaged
        raise LLMBackendError(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise LLMBackendError(f"reply parsed to {type(parsed).__name__}, want list")
    rows = [row for row in parsed if isinstance(row, dict)]
    if not rows:
        raise LLMBackendError("reply contained no JSON objects")
    return rows
