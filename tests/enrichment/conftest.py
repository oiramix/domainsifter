"""Shared pytest fixtures for enrichment tests.

Each enrichment source instantiates a module-level CircuitBreaker. Without
a reset between tests, failure-mocking tests would leak state into the next
test (5+ consecutive simulated failures would open the breaker and the next
test's mocked happy-path would silently bypass the network call entirely).

This autouse fixture imports each source's `_BREAKER` and resets it both
before and after every test, making test ordering irrelevant.
"""

from __future__ import annotations

import importlib

import pytest

_BREAKER_MODULES = (
    "scripts.enrichment.wayback",
    "scripts.enrichment.open_page_rank",
    "scripts.enrichment.spam_check",
    "scripts.enrichment.crtsh",
    "scripts.enrichment.rdap",
    "scripts.enrichment.surbl",
    "scripts.enrichment.spamhaus",
)


@pytest.fixture(autouse=True)
def _reset_breakers():
    # Reset all per-source breakers, the global host throttle, AND the
    # global per-host 429 cooldown so one test's simulated rate-limit
    # state doesn't leak into the next.
    from scripts.enrichment._circuit_breaker import (
        GLOBAL_HOST_COOLDOWN,
        GLOBAL_HOST_STOP,
        GLOBAL_HOST_STRIKES,
        GLOBAL_HOST_THROTTLE,
    )
    for path in _BREAKER_MODULES:
        mod = importlib.import_module(path)
        breaker = getattr(mod, "_BREAKER", None)
        if breaker is not None:
            breaker.reset()
    GLOBAL_HOST_THROTTLE.reset()
    GLOBAL_HOST_COOLDOWN.reset()
    GLOBAL_HOST_STOP.reset()
    GLOBAL_HOST_STRIKES.reset()
    yield
    for path in _BREAKER_MODULES:
        mod = importlib.import_module(path)
        breaker = getattr(mod, "_BREAKER", None)
        if breaker is not None:
            breaker.reset()
    GLOBAL_HOST_THROTTLE.reset()
    GLOBAL_HOST_COOLDOWN.reset()
    GLOBAL_HOST_STOP.reset()
    GLOBAL_HOST_STRIKES.reset()
