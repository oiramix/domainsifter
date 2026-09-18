"""Unit tests for scripts/llm_backend.py.

Nothing here touches a live API, a real `claude` binary, or the network.
`subprocess.run` is always mocked (hard rule 13) and the `anthropic` SDK is
injected into ``sys.modules`` so these tests pass whether or not the real SDK
is installed.

Coverage map:
  _read_env_file        — KEY=VALUE parsing, comments/blanks/malformed lines,
                          missing file, and the deliberate NO-quote-stripping
                          (systemd EnvironmentFile= semantics).
  ClaudeCodeBackend     — argv shape, envelope["result"] on success, and the
                          security-critical child environment: API key and
                          auth token scrubbed, MAX_THINKING_TOKENS=0, HOME
                          defaulted, OAuth token never in argv.
  failure paths         — non-zero exit, timeout, OSError, non-JSON stdout,
                          is_error envelope, non-success subtype, empty result;
                          every one raises LLMBackendError.
  ApiBackend            — missing key, happy path text-block join.
  get_backend           — name dispatch, case/whitespace tolerance, unknown
                          name, config override vs DEFAULTS.
  parse_json_array      — plain / fenced / chatter-wrapped arrays, and the
                          load-bearing case: prose or a refusal is a hard
                          failure, never an empty result.

All domain names used below are invented (hard rule 1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from scripts import llm_backend as lb

# Invented domains only — hard rule 1.
DOMAIN_A = "marketglow.com"
DOMAIN_B = "tideblock.io"
DOMAIN_C = "coppernest.org"

FAKE_OAUTH_TOKEN = "sk-ant-oat01-TESTTOKEN-not-a-real-credential"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(result: str = "[]", **overrides) -> str:
    """A minimal well-formed `claude -p --output-format json` envelope."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "duration_api_ms": 1234,
        "usage": {"output_tokens": 42, "cache_read_input_tokens": 25000},
    }
    base.update(overrides)
    return json.dumps(base)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Stand-in for subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _write_env_file(tmp_path, body: str) -> str:
    path = tmp_path / "claude.env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _backend(tmp_path, **llm) -> lb.ClaudeCodeBackend:
    cfg = {"llm": {"binary": "/usr/bin/claude", "model": "haiku"}}
    cfg["llm"].setdefault("env_file", str(tmp_path / "missing.env"))
    cfg["llm"].update(llm)
    return lb.ClaudeCodeBackend(cfg)


class _TextBlock:
    """A content block with a `.text` attribute (MagicMock would match
    hasattr() unconditionally, which would defeat the filter under test)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _OtherBlock:
    """A content block with no `.text` — e.g. a thinking or tool_use block."""

    def __init__(self) -> None:
        self.type = "thinking"


# ---------------------------------------------------------------------------
# _read_env_file
# ---------------------------------------------------------------------------


def test_read_env_file_parses_key_value_pairs(tmp_path):
    path = _write_env_file(
        tmp_path, "CLAUDE_CODE_OAUTH_TOKEN=abc123\nANTHROPIC_MODEL=haiku\n"
    )
    assert lb._read_env_file(path) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "abc123",
        "ANTHROPIC_MODEL": "haiku",
    }


def test_read_env_file_ignores_blanks_comments_and_malformed_lines(tmp_path):
    path = _write_env_file(
        tmp_path,
        "\n"
        "# a comment\n"
        "   \n"
        "GOOD=value\n"
        "this line has no equals sign\n"
        "   # indented comment\n"
        "\t\n"
        "ALSO_GOOD=second\n",
    )
    assert lb._read_env_file(path) == {"GOOD": "value", "ALSO_GOOD": "second"}


def test_read_env_file_missing_file_returns_empty_dict(tmp_path):
    assert lb._read_env_file(str(tmp_path / "definitely-absent.env")) == {}


def test_read_env_file_unreadable_directory_returns_empty_dict(tmp_path):
    # Opening a directory raises OSError (IsADirectoryError / PermissionError
    # on Windows) — the same swallow path as a missing file.
    assert lb._read_env_file(str(tmp_path)) == {}


def test_read_env_file_does_not_strip_quotes(tmp_path):
    """systemd EnvironmentFile= semantics: the value is taken literally."""
    path = _write_env_file(
        tmp_path, 'DQ="quoted"\nSQ=\'single\'\nBARE=plain\n'
    )
    parsed = lb._read_env_file(path)
    assert parsed["DQ"] == '"quoted"'
    assert parsed["SQ"] == "'single'"
    assert parsed["BARE"] == "plain"


def test_read_env_file_keeps_equals_signs_inside_the_value(tmp_path):
    path = _write_env_file(tmp_path, "TOKEN=a=b=c\n")
    assert lb._read_env_file(path) == {"TOKEN": "a=b=c"}


def test_read_env_file_strips_key_whitespace_but_not_value_leading_space(tmp_path):
    path = _write_env_file(tmp_path, "  SPACED  = value \n")
    # Key is .strip()ed; the value keeps its leading space because only the
    # whole line was stripped, never the value itself.
    assert lb._read_env_file(path) == {"SPACED": " value"}


# ---------------------------------------------------------------------------
# ClaudeCodeBackend.complete — argv and happy path
# ---------------------------------------------------------------------------


def test_complete_builds_expected_argv(tmp_path):
    backend = _backend(tmp_path, binary="/opt/claude", model="sonnet")
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(_envelope(result="OK"))
        assert backend.complete(system="SYS PROMPT", user="USER PROMPT") == "OK"

    argv = run.call_args.args[0]
    assert argv == [
        "/opt/claude",
        "-p",
        "USER PROMPT",
        "--model",
        "sonnet",
        "--output-format",
        "json",
        "--append-system-prompt",
        "SYS PROMPT",
    ]
    kwargs = run.call_args.kwargs
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


def test_complete_returns_envelope_result(tmp_path):
    payload = json.dumps([{"domain": DOMAIN_A, "score": 81}])
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(_envelope(result=payload))
        assert backend.complete(system="s", user="u") == payload


def test_complete_uses_config_timeout_by_default(tmp_path):
    backend = _backend(tmp_path, timeout_seconds=123)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(_envelope(result="ok"))
        backend.complete(system="s", user="u")
    assert run.call_args.kwargs["timeout"] == 123


def test_complete_per_call_timeout_overrides_config(tmp_path):
    backend = _backend(tmp_path, timeout_seconds=900)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(_envelope(result="ok"))
        backend.complete(system="s", user="u", timeout_seconds=30)
    assert run.call_args.kwargs["timeout"] == 30


def test_complete_defaults_come_from_DEFAULTS_when_config_empty():
    backend = lb.ClaudeCodeBackend({})
    with patch.object(lb, "_read_env_file", return_value={}), patch.object(
        lb.subprocess, "run"
    ) as run:
        run.return_value = _completed(_envelope(result="ok"))
        backend.complete(system="s", user="u")
    argv = run.call_args.args[0]
    assert argv[0] == lb.DEFAULTS["binary"]
    assert argv[argv.index("--model") + 1] == lb.DEFAULTS["model"]
    assert run.call_args.kwargs["timeout"] == lb.DEFAULTS["timeout_seconds"]


# ---------------------------------------------------------------------------
# ClaudeCodeBackend — security-critical child environment
# ---------------------------------------------------------------------------


def _run_and_capture_env(tmp_path, parent_env: dict, env_body: str) -> tuple:
    """Run complete() with a scripted parent env + env file; return
    (child_env, argv)."""
    path = _write_env_file(tmp_path, env_body)
    backend = _backend(tmp_path, env_file=path)
    with patch.dict(os.environ, parent_env, clear=True), patch.object(
        lb.subprocess, "run"
    ) as run:
        run.return_value = _completed(_envelope(result="ok"))
        backend.complete(system="s", user="u")
    return run.call_args.kwargs["env"], run.call_args.args[0]


def test_child_env_scrubs_anthropic_api_key_and_auth_token(tmp_path):
    child_env, _ = _run_and_capture_env(
        tmp_path,
        {
            "ANTHROPIC_API_KEY": "sk-ant-api03-should-be-scrubbed",
            "ANTHROPIC_AUTH_TOKEN": "should-also-be-scrubbed",
            "PATH": "/usr/bin",
        },
        f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH_TOKEN}\n",
    )
    # Trap 1: the API key outranks the OAuth token in Claude Code's auth
    # precedence, so it MUST NOT reach the child.
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "ANTHROPIC_AUTH_TOKEN" not in child_env
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH_TOKEN
    assert child_env["PATH"] == "/usr/bin"


def test_child_env_scrubs_api_key_even_when_it_comes_from_the_env_file(tmp_path):
    child_env, _ = _run_and_capture_env(
        tmp_path,
        {"PATH": "/usr/bin"},
        "ANTHROPIC_API_KEY=sk-ant-api03-from-file\n"
        f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH_TOKEN}\n",
    )
    assert "ANTHROPIC_API_KEY" not in child_env
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH_TOKEN


def test_child_env_disables_extended_thinking(tmp_path):
    child_env, _ = _run_and_capture_env(
        tmp_path, {"MAX_THINKING_TOKENS": "16000"}, ""
    )
    assert child_env["MAX_THINKING_TOKENS"] == "0"


def test_child_env_defaults_home_when_unset(tmp_path):
    child_env, _ = _run_and_capture_env(tmp_path, {"PATH": "/usr/bin"}, "")
    assert child_env["HOME"] == "/home/domainsifter"


def test_child_env_preserves_an_existing_home(tmp_path):
    child_env, _ = _run_and_capture_env(
        tmp_path, {"HOME": "/var/lib/domainsifter"}, ""
    )
    assert child_env["HOME"] == "/var/lib/domainsifter"


def test_env_file_overrides_the_parent_environment(tmp_path):
    child_env, _ = _run_and_capture_env(
        tmp_path,
        {"CLAUDE_CODE_OAUTH_TOKEN": "stale-token-from-parent"},
        f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH_TOKEN}\n",
    )
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH_TOKEN


def test_oauth_token_never_appears_in_argv(tmp_path):
    """The token rides in the child env only — argv is visible in `ps`."""
    child_env, argv = _run_and_capture_env(
        tmp_path, {}, f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_OAUTH_TOKEN}\n"
    )
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_OAUTH_TOKEN
    assert all(FAKE_OAUTH_TOKEN not in str(arg) for arg in argv)
    assert FAKE_OAUTH_TOKEN not in " ".join(str(a) for a in argv)


def test_child_env_is_a_copy_and_does_not_mutate_os_environ(tmp_path):
    parent = {"ANTHROPIC_API_KEY": "sk-ant-api03-keepme", "PATH": "/usr/bin"}
    with patch.dict(os.environ, parent, clear=True):
        backend = _backend(tmp_path)
        with patch.object(lb.subprocess, "run") as run:
            run.return_value = _completed(_envelope(result="ok"))
            backend.complete(system="s", user="u")
        # The scrub happens on the copy, not on the pipeline's own process.
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-keepme"
        assert "MAX_THINKING_TOKENS" not in os.environ


# ---------------------------------------------------------------------------
# ClaudeCodeBackend — failure paths (all must raise LLMBackendError)
# ---------------------------------------------------------------------------


def test_complete_raises_on_non_zero_returncode(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed("", "auth failed", returncode=1)
        with pytest.raises(lb.LLMBackendError, match="exited 1"):
            backend.complete(system="s", user="u")


def test_complete_raises_on_timeout(tmp_path):
    backend = _backend(tmp_path, timeout_seconds=900)
    with patch.object(lb.subprocess, "run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=900)
        with pytest.raises(lb.LLMBackendError, match="timed out after 900s"):
            backend.complete(system="s", user="u")


def test_complete_raises_when_binary_is_missing(tmp_path):
    backend = _backend(tmp_path, binary="/nowhere/claude")
    with patch.object(lb.subprocess, "run") as run:
        run.side_effect = FileNotFoundError(2, "No such file or directory")
        with pytest.raises(lb.LLMBackendError, match="cannot execute /nowhere/claude"):
            backend.complete(system="s", user="u")


def test_complete_raises_on_generic_oserror(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.side_effect = OSError("Exec format error")
        with pytest.raises(lb.LLMBackendError, match="cannot execute"):
            backend.complete(system="s", user="u")


def test_complete_raises_on_non_json_stdout(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed("Welcome to Claude Code!\nnot json at all")
        with pytest.raises(lb.LLMBackendError, match="non-JSON envelope"):
            backend.complete(system="s", user="u")


def test_complete_raises_when_envelope_is_error(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(
            _envelope(result="whatever", is_error=True, api_error_status=529)
        )
        with pytest.raises(lb.LLMBackendError, match="reported failure"):
            backend.complete(system="s", user="u")


def test_complete_raises_when_subtype_is_not_success(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(
            _envelope(result="partial", subtype="error_max_turns")
        )
        with pytest.raises(lb.LLMBackendError, match="error_max_turns"):
            backend.complete(system="s", user="u")


def test_complete_raises_when_subtype_missing(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        envelope = json.loads(_envelope(result="text"))
        envelope.pop("subtype")
        run.return_value = _completed(json.dumps(envelope))
        with pytest.raises(lb.LLMBackendError, match="reported failure"):
            backend.complete(system="s", user="u")


@pytest.mark.parametrize("result", ["", "   \n\t ", None, 42, [], {"a": 1}])
def test_complete_raises_on_empty_or_non_string_result(tmp_path, result):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(_envelope(result=result))
        with pytest.raises(lb.LLMBackendError, match="empty result"):
            backend.complete(system="s", user="u")


def test_complete_raises_when_result_key_absent(tmp_path):
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        envelope = json.loads(_envelope())
        envelope.pop("result")
        run.return_value = _completed(json.dumps(envelope))
        with pytest.raises(lb.LLMBackendError, match="empty result"):
            backend.complete(system="s", user="u")


def test_backend_error_is_a_runtime_error():
    """Callers catch LLMBackendError; it must stay a RuntimeError subclass."""
    assert issubclass(lb.LLMBackendError, RuntimeError)


# ---------------------------------------------------------------------------
# ApiBackend
# ---------------------------------------------------------------------------


def _fake_anthropic(message=None, *, raises: Exception | None = None):
    """A stand-in `anthropic` module for sys.modules injection."""
    module = types.ModuleType("anthropic")
    client = MagicMock(name="AnthropicClient")
    if raises is not None:
        client.messages.create.side_effect = raises
    else:
        client.messages.create.return_value = message
    module.Anthropic = MagicMock(name="Anthropic", return_value=client)
    return module, client


def test_api_backend_raises_when_api_key_absent():
    module, client = _fake_anthropic(MagicMock())
    backend = lb.ApiBackend({})
    with patch.dict(sys.modules, {"anthropic": module}), patch.dict(
        os.environ, {}, clear=True
    ):
        with pytest.raises(lb.LLMBackendError, match="ANTHROPIC_API_KEY is not set"):
            backend.complete(system="s", user="u")
    client.messages.create.assert_not_called()


def test_api_backend_raises_when_api_key_is_empty_string():
    module, _ = _fake_anthropic(MagicMock())
    backend = lb.ApiBackend({})
    with patch.dict(sys.modules, {"anthropic": module}), patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True
    ):
        with pytest.raises(lb.LLMBackendError, match="ANTHROPIC_API_KEY is not set"):
            backend.complete(system="s", user="u")


def test_api_backend_joins_text_blocks_on_success():
    message = MagicMock()
    message.content = [
        _TextBlock('[{"domain": "' + DOMAIN_B + '", '),
        _OtherBlock(),
        _TextBlock('"score": 64}]'),
    ]
    module, client = _fake_anthropic(message)
    backend = lb.ApiBackend({"llm": {"api_model": "m-1", "api_max_tokens": 555}})
    with patch.dict(sys.modules, {"anthropic": module}), patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api03-test"}, clear=True
    ):
        text = backend.complete(system="SYS", user="USER")

    assert text == '[{"domain": "' + DOMAIN_B + '", "score": 64}]'
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "m-1"
    assert kwargs["max_tokens"] == 555
    assert kwargs["system"] == "SYS"
    assert kwargs["messages"] == [{"role": "user", "content": "USER"}]


def test_api_backend_passes_timeout_to_the_client():
    message = MagicMock()
    message.content = [_TextBlock("ok")]
    module, _ = _fake_anthropic(message)
    backend = lb.ApiBackend({"llm": {"timeout_seconds": 600}})
    with patch.dict(sys.modules, {"anthropic": module}), patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=True
    ):
        backend.complete(system="s", user="u", timeout_seconds=45)
    assert module.Anthropic.call_args.kwargs["timeout"] == 45.0


def test_api_backend_wraps_sdk_exceptions():
    module, _ = _fake_anthropic(None, raises=ValueError("credit balance too low"))
    backend = lb.ApiBackend({})
    with patch.dict(sys.modules, {"anthropic": module}), patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=True
    ):
        with pytest.raises(lb.LLMBackendError, match="credit balance too low"):
            backend.complete(system="s", user="u")


def test_api_backend_raises_on_empty_message():
    message = MagicMock()
    message.content = [_TextBlock("   ")]
    module, _ = _fake_anthropic(message)
    backend = lb.ApiBackend({})
    with patch.dict(sys.modules, {"anthropic": module}), patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=True
    ):
        with pytest.raises(lb.LLMBackendError, match="empty message"):
            backend.complete(system="s", user="u")


# ---------------------------------------------------------------------------
# cfg / get_backend
# ---------------------------------------------------------------------------


def test_cfg_falls_back_to_defaults():
    assert lb.cfg({}, "backend") == lb.DEFAULTS["backend"]
    assert lb.cfg({"llm": {}}, "model") == lb.DEFAULTS["model"]
    assert lb.cfg(None, "timeout_seconds") == lb.DEFAULTS["timeout_seconds"]


def test_cfg_prefers_config_over_defaults():
    assert lb.cfg({"llm": {"model": "opus"}}, "model") == "opus"


@pytest.mark.parametrize("name", ["claude_code", "CLAUDE_CODE", "  Claude_Code  "])
def test_get_backend_returns_claude_code(name):
    backend = lb.get_backend({"llm": {"backend": name}})
    assert isinstance(backend, lb.ClaudeCodeBackend)
    assert backend.name == "claude_code"


@pytest.mark.parametrize("name", ["api", "API", " Api\t"])
def test_get_backend_returns_api(name):
    backend = lb.get_backend({"llm": {"backend": name}})
    assert isinstance(backend, lb.ApiBackend)
    assert backend.name == "api"


def test_get_backend_defaults_to_claude_code_when_unconfigured():
    assert isinstance(lb.get_backend({}), lb.ClaudeCodeBackend)
    assert lb.DEFAULTS["backend"] == "claude_code"


@pytest.mark.parametrize("name", ["", "openai", "claude-code", "claudecode", "apix"])
def test_get_backend_rejects_unknown_names(name):
    with pytest.raises(lb.LLMBackendError, match="unknown llm.backend"):
        lb.get_backend({"llm": {"backend": name}})


def test_get_backend_threads_config_overrides_into_the_instance():
    backend = lb.get_backend(
        {
            "llm": {
                "backend": "claude_code",
                "binary": "/opt/bin/claude",
                "model": "opus",
                "timeout_seconds": 60,
                "env_file": "/etc/other.env",
            }
        }
    )
    assert backend._binary == "/opt/bin/claude"
    assert backend._model == "opus"
    assert backend._timeout == 60
    assert backend._env_file == "/etc/other.env"


def test_get_backend_api_uses_defaults_for_unset_keys():
    backend = lb.get_backend({"llm": {"backend": "api"}})
    assert backend._model == lb.DEFAULTS["api_model"]
    assert backend._max_tokens == lb.DEFAULTS["api_max_tokens"]
    assert backend._timeout == lb.DEFAULTS["timeout_seconds"]


# ---------------------------------------------------------------------------
# parse_json_array
# ---------------------------------------------------------------------------


def test_parse_json_array_plain_array():
    text = json.dumps(
        [{"domain": DOMAIN_A, "score": 88}, {"domain": DOMAIN_B, "score": 12}]
    )
    rows = lb.parse_json_array(text)
    assert [row["domain"] for row in rows] == [DOMAIN_A, DOMAIN_B]


def test_parse_json_array_json_fenced():
    text = f'```json\n[{{"domain": "{DOMAIN_C}", "score": 50}}]\n```'
    assert lb.parse_json_array(text) == [{"domain": DOMAIN_C, "score": 50}]


def test_parse_json_array_fence_without_language():
    text = f'```\n[{{"domain": "{DOMAIN_C}"}}]\n```'
    assert lb.parse_json_array(text) == [{"domain": DOMAIN_C}]


def test_parse_json_array_embedded_in_chatter():
    text = (
        "Sure — here are the ranked names you asked for:\n\n"
        f'[{{"domain": "{DOMAIN_A}", "score": 71}}]\n\n'
        "Let me know if you'd like a different weighting."
    )
    assert lb.parse_json_array(text) == [{"domain": DOMAIN_A, "score": 71}]


def test_parse_json_array_fenced_inside_chatter():
    text = (
        "Here you go:\n"
        "```json\n"
        f'[{{"domain": "{DOMAIN_B}", "score": 33}}]\n'
        "```\n"
        "Hope that helps!"
    )
    assert lb.parse_json_array(text) == [{"domain": DOMAIN_B, "score": 33}]


@pytest.mark.parametrize(
    "prose",
    [
        "I can't help with assessing trademark registrability for these names.",
        "I'm unable to complete this request.",
        "There were no suitable candidates in this batch.",
        "",
        "   ",
    ],
)
def test_parse_json_array_prose_or_refusal_is_a_hard_failure(prose):
    """Load-bearing: a refusal must NOT be mistaken for 'no results'."""
    with pytest.raises(lb.LLMBackendError, match="not a JSON array"):
        lb.parse_json_array(prose)


def test_parse_json_array_rejects_reversed_brackets():
    with pytest.raises(lb.LLMBackendError, match="not a JSON array"):
        lb.parse_json_array("] nonsense [")


def test_parse_json_array_invalid_json_raises():
    text = '[{"domain": "' + DOMAIN_A + '", "score": 88,}]'  # trailing comma
    with pytest.raises(lb.LLMBackendError, match="not valid JSON"):
        lb.parse_json_array(text)


def test_parse_json_array_truncated_output_raises():
    text = '[{"domain": "' + DOMAIN_A + '", "score": 8'
    with pytest.raises(lb.LLMBackendError, match="not a JSON array"):
        lb.parse_json_array(text)


def test_parse_json_array_rejects_a_json_object():
    text = json.dumps({"domain": DOMAIN_A, "score": 88})
    with pytest.raises(lb.LLMBackendError):
        lb.parse_json_array(text)


def test_parse_json_array_rejects_object_wrapping_an_array():
    # Bracket-slicing recovers the inner array, so this still yields rows —
    # what matters is that it never returns a bare object.
    text = json.dumps({"results": [{"domain": DOMAIN_B}]})
    assert lb.parse_json_array(text) == [{"domain": DOMAIN_B}]


def test_parse_json_array_rejects_array_of_non_dicts():
    with pytest.raises(lb.LLMBackendError, match="no JSON objects"):
        lb.parse_json_array(json.dumps([DOMAIN_A, DOMAIN_B, 3]))


def test_parse_json_array_rejects_empty_array():
    with pytest.raises(lb.LLMBackendError, match="no JSON objects"):
        lb.parse_json_array("[]")


def test_parse_json_array_keeps_only_dicts_in_a_mixed_array():
    text = json.dumps(
        [{"domain": DOMAIN_A}, "junk", 7, None, [1, 2], {"domain": DOMAIN_C}]
    )
    assert lb.parse_json_array(text) == [{"domain": DOMAIN_A}, {"domain": DOMAIN_C}]


# ---------------------------------------------------------------------------
# End-to-end wiring (still no real subprocess)
# ---------------------------------------------------------------------------


def test_claude_code_result_feeds_parse_json_array(tmp_path):
    payload = f'```json\n[{{"domain": "{DOMAIN_A}", "score": 90}}]\n```'
    backend = _backend(tmp_path)
    with patch.object(lb.subprocess, "run") as run:
        run.return_value = _completed(_envelope(result=payload))
        rows = lb.parse_json_array(backend.complete(system="s", user="u"))
    assert rows == [{"domain": DOMAIN_A, "score": 90}]
