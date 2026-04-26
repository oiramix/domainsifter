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
