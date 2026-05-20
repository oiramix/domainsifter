"""Unit tests for scripts/env_check.py."""

from __future__ import annotations

import logging

import pytest

from scripts import env_check
from scripts.env_check import MissingEnvVarsError


REQUIRED = {
    "CZDS_USERNAME": "u",
    "CZDS_PASSWORD": "p",
    "SAFE_BROWSING_KEY": "k",
    "R2_ACCOUNT_ID": "acct",
    "R2_ACCESS_KEY_ID": "ak",
    "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET_NAME": "domainsifter-state",
}


def test_validate_env_passes_when_all_required_set():
    env = {**REQUIRED, "OPENPAGERANK_KEY": "o"}
    env_check.validate_env(env)


def test_validate_env_passes_when_only_required_set():
    env_check.validate_env(dict(REQUIRED))


def test_validate_env_raises_listing_all_missing():
    with pytest.raises(MissingEnvVarsError) as exc:
        env_check.validate_env({})
    assert exc.value.missing == [
        "CZDS_USERNAME",
        "CZDS_PASSWORD",
        "SAFE_BROWSING_KEY",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
    ]


def test_validate_env_raises_for_partially_missing():
    env = dict(REQUIRED)
    del env["CZDS_PASSWORD"]
    del env["R2_BUCKET_NAME"]
    with pytest.raises(MissingEnvVarsError) as exc:
        env_check.validate_env(env)
    assert exc.value.missing == ["CZDS_PASSWORD", "R2_BUCKET_NAME"]


def test_validate_env_treats_empty_string_as_missing():
    env = {**REQUIRED, "R2_ACCESS_KEY_ID": ""}
    with pytest.raises(MissingEnvVarsError) as exc:
        env_check.validate_env(env)
    assert exc.value.missing == ["R2_ACCESS_KEY_ID"]


def test_validate_env_warns_when_optional_missing(caplog):
    with caplog.at_level(logging.WARNING, logger="scripts.env_check"):
        env_check.validate_env(dict(REQUIRED))
    assert any("OPENPAGERANK_KEY" in rec.message for rec in caplog.records)


def test_validate_env_falls_back_to_os_environ_when_arg_omitted(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OPENPAGERANK_KEY", "o")
    env_check.validate_env()


# --- ANTHROPIC_API_KEY soft-fail (Phase 4 wire-in, 2026-05-20) -------------


def test_anthropic_key_missing_warns_with_custom_message(caplog):
    """Per Phase 4 design decision (k): missing key is SOFT-FAIL with a
    LOUD WARNING. validate_env must not raise; the warning must contain
    the specific phrase 'snapshot classification disabled' so the daily
    report email surfaces misconfiguration."""
    env = dict(REQUIRED)  # no ANTHROPIC_API_KEY
    with caplog.at_level(logging.WARNING, logger="scripts.env_check"):
        env_check.validate_env(env)
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert any(
        "ANTHROPIC_API_KEY" in rec.message
        and "snapshot classification disabled" in rec.message
        for rec in warnings
    ), (
        "Expected a WARNING citing ANTHROPIC_API_KEY + 'snapshot "
        "classification disabled'; got: "
        + repr([rec.message for rec in warnings])
    )


def test_anthropic_key_present_logs_info_not_warning(caplog):
    """When the key IS set, no warning; info-level confirmation only."""
    env = {**REQUIRED, "ANTHROPIC_API_KEY": "sk-ant-test"}
    with caplog.at_level(logging.DEBUG, logger="scripts.env_check"):
        env_check.validate_env(env)
    anthropic_warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "ANTHROPIC_API_KEY" in rec.message
    ]
    assert anthropic_warnings == []
    info_records = [
        rec for rec in caplog.records
        if rec.levelname == "INFO" and "ANTHROPIC_API_KEY" in rec.message
    ]
    assert info_records, "Expected an INFO line confirming the key is set."


def test_anthropic_key_empty_string_treated_as_missing(caplog):
    """Empty value treated as missing — matches the OPTIONAL var check
    using truthiness (`if not source.get(name)`)."""
    env = {**REQUIRED, "ANTHROPIC_API_KEY": ""}
    with caplog.at_level(logging.WARNING, logger="scripts.env_check"):
        env_check.validate_env(env)
    assert any(
        "snapshot classification disabled" in rec.message
        for rec in caplog.records
    )


def test_anthropic_key_missing_does_not_raise():
    """Hard-required vars raise MissingEnvVarsError; ANTHROPIC_API_KEY
    must NOT. The pipeline must run without it (soft-fail design)."""
    env = dict(REQUIRED)  # no ANTHROPIC_API_KEY
    env_check.validate_env(env)  # no exception
