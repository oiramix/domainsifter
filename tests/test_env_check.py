"""Unit tests for scripts/env_check.py."""

from __future__ import annotations

import logging

import pytest

from scripts import env_check
from scripts.env_check import MissingEnvVarsError


def test_validate_env_passes_when_all_required_set():
    env = {
        "CZDS_USERNAME": "u",
        "CZDS_PASSWORD": "p",
        "SAFE_BROWSING_KEY": "k",
        "OPENPAGERANK_KEY": "o",
    }
    env_check.validate_env(env)


def test_validate_env_passes_when_only_required_set():
    env = {
        "CZDS_USERNAME": "u",
        "CZDS_PASSWORD": "p",
        "SAFE_BROWSING_KEY": "k",
    }
    env_check.validate_env(env)


def test_validate_env_raises_listing_all_missing():
    with pytest.raises(MissingEnvVarsError) as exc:
        env_check.validate_env({})
    assert exc.value.missing == ["CZDS_USERNAME", "CZDS_PASSWORD", "SAFE_BROWSING_KEY"]


def test_validate_env_raises_for_partially_missing():
    env = {"CZDS_USERNAME": "u", "SAFE_BROWSING_KEY": "k"}
    with pytest.raises(MissingEnvVarsError) as exc:
        env_check.validate_env(env)
    assert exc.value.missing == ["CZDS_PASSWORD"]


def test_validate_env_treats_empty_string_as_missing():
    env = {"CZDS_USERNAME": "u", "CZDS_PASSWORD": "", "SAFE_BROWSING_KEY": "k"}
    with pytest.raises(MissingEnvVarsError) as exc:
        env_check.validate_env(env)
    assert exc.value.missing == ["CZDS_PASSWORD"]


def test_validate_env_warns_when_optional_missing(caplog):
    env = {"CZDS_USERNAME": "u", "CZDS_PASSWORD": "p", "SAFE_BROWSING_KEY": "k"}
    with caplog.at_level(logging.WARNING, logger="scripts.env_check"):
        env_check.validate_env(env)
    assert any("OPENPAGERANK_KEY" in rec.message for rec in caplog.records)


def test_validate_env_falls_back_to_os_environ_when_arg_omitted(monkeypatch):
    monkeypatch.setenv("CZDS_USERNAME", "u")
    monkeypatch.setenv("CZDS_PASSWORD", "p")
    monkeypatch.setenv("SAFE_BROWSING_KEY", "k")
    monkeypatch.setenv("OPENPAGERANK_KEY", "o")
    env_check.validate_env()
