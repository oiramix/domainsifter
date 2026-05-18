"""Unit tests for scripts/snapshot_classifier.py.

All Anthropic + Wayback calls mocked. No network, no real API key needed.
The classifier itself has no module-level mutable state, so tests are
order-independent.

Coverage:
  - Each of the four valid category outputs lands correctly
  - Garbage Haiku output → unknown (with log warning)
  - Haiku raises → unknown (with log warning)
  - fetch_excerpt returns None → unknown
  - fetch_excerpt raises → unknown
  - No wayback_last_snapshot → unknown without any fetch attempted
  - client=None → all unknown without any fetch attempted
  - snapshot_classifier_version stamped on every record the classifier touched
  - _parse_classification handles trailing punctuation / case
  - _build_user_message includes only the four content fields
  - classify_all sleep cadence (only between, not after last)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts import snapshot_classifier as sc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """In-test stand-in for ClassifierClient. Returns scripted responses;
    optionally raises on the Nth call. Records all user messages it received."""

    def __init__(self, responses, *, raises_on: set[int] | None = None):
        self._responses = list(responses)
        self._raises_on = raises_on or set()
        self._call_count = 0
        self.calls: list[str] = []

    def classify(self, user: str) -> str:
        self._call_count += 1
        self.calls.append(user)
        if self._call_count in self._raises_on:
            raise RuntimeError(f"simulated failure on call {self._call_count}")
        if not self._responses:
            return "legitimate"
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Globally stub out time.sleep — the per-candidate pause adds nothing
    to tests but wall-clock cost."""
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)


@pytest.fixture
def good_excerpt():
    """A minimal-but-realistic excerpt with all four fields populated."""
    return {
        "snapshot_timestamp": "20251215120000",
        "snapshot_url": "http://web.archive.org/web/20251215120000/http://example.com/",
        "title": "Acme Roofing — Boston MA",
        "meta_description": "Family-owned roofers since 1985.",
        "h1": ["Acme Roofing"],
        "h2": ["Our services", "Service area"],
    }


def _stub_fetch(monkeypatch, by_name):
    """Replace fetch_excerpt with a deterministic in-memory lookup.

    `by_name` maps domain name → excerpt-dict | None | callable. A callable
    is invoked with (name, target_date) — used for raises_on tests.
    """
    def _stub(name, target_date):
        v = by_name.get(name)
        if callable(v):
            return v(name, target_date)
        return v
    monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _stub)


# ---------------------------------------------------------------------------
# _parse_classification
# ---------------------------------------------------------------------------


class TestParseClassification:
    def test_valid_categories_pass_through(self):
        for cat in ("legitimate", "parked", "toxic", "empty"):
            assert sc._parse_classification(cat) == cat

    def test_uppercase_normalized(self):
        assert sc._parse_classification("LEGITIMATE") == "legitimate"
        assert sc._parse_classification("Parked") == "parked"

    def test_trailing_whitespace_stripped(self):
        assert sc._parse_classification("  toxic  \n") == "toxic"

    def test_trailing_punctuation_stripped(self):
        assert sc._parse_classification("empty.") == "empty"
        assert sc._parse_classification("legitimate!") == "legitimate"
        assert sc._parse_classification("parked;") == "parked"

    def test_garbage_word_returns_unknown(self):
        assert sc._parse_classification("malicious") == sc.UNKNOWN_CATEGORY
        assert sc._parse_classification("safe") == sc.UNKNOWN_CATEGORY

    def test_empty_string_returns_unknown(self):
        assert sc._parse_classification("") == sc.UNKNOWN_CATEGORY
        assert sc._parse_classification("   ") == sc.UNKNOWN_CATEGORY

    def test_multiword_response_returns_unknown(self):
        # The prompt forbids reasoning; if the model emits "this is legitimate"
        # we treat it as unknown rather than try to extract a word.
        assert sc._parse_classification("this is legitimate") == sc.UNKNOWN_CATEGORY


# ---------------------------------------------------------------------------
# _build_user_message
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    def test_only_content_fields_included(self, good_excerpt):
        msg = sc._build_user_message(good_excerpt)
        assert "snapshot_timestamp" not in msg
        assert "snapshot_url" not in msg
        assert "Acme Roofing" in msg
        assert "title" in msg
        assert "meta_description" in msg
        assert "h1" in msg
        assert "h2" in msg

    def test_non_latin_preserved_as_chars_not_escapes(self):
        excerpt = {"title": "月見うどん専門店", "meta_description": None,
                   "h1": [], "h2": []}
        msg = sc._build_user_message(excerpt)
        assert "月見" in msg
        # Compact JSON, no \uXXXX escapes
        assert "\\u" not in msg

    def test_missing_lists_default_to_empty(self):
        excerpt = {"title": "X", "meta_description": "Y"}  # no h1/h2 keys
        msg = sc._build_user_message(excerpt)
        assert '"h1":[]' in msg
        assert '"h2":[]' in msg

    def test_compact_no_indent(self, good_excerpt):
        msg = sc._build_user_message(good_excerpt)
        assert "\n" not in msg  # compact mode
        # No spaces after separators
        assert '","' in msg or ": " not in msg


# ---------------------------------------------------------------------------
# classify_one — every failure path lands on UNKNOWN_CATEGORY
# ---------------------------------------------------------------------------


class TestClassifyOneFailurePaths:
    def test_no_wayback_last_snapshot_skips_fetch(self, monkeypatch):
        # If classify_one tries to call fetch_excerpt despite no snapshot
        # date, this stub raises so the test fails.
        def _must_not_call(name, target_date):
            raise AssertionError("fetch_excerpt should not be called without snapshot date")
        monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _must_not_call)

        record = {"name": "noshot.net"}
        client = _FakeClient([])
        result = sc.classify_one(record, client=client)

        assert result == sc.UNKNOWN_CATEGORY
        assert record["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] is None
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION
        assert client.calls == []  # no Haiku call either

    def test_fetch_returns_none_yields_unknown(self, monkeypatch):
        _stub_fetch(monkeypatch, {"noshot.net": None})
        record = {"name": "noshot.net", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient([])
        result = sc.classify_one(record, client=client)

        assert result == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] is None
        assert client.calls == []  # no Haiku call when no excerpt

    def test_fetch_raises_yields_unknown(self, monkeypatch, caplog):
        def _boom(name, target_date):
            raise RuntimeError("bs4 internal error")
        _stub_fetch(monkeypatch, {"boom.net": _boom})

        record = {"name": "boom.net", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient([])
        with caplog.at_level("WARNING"):
            result = sc.classify_one(record, client=client)

        assert result == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] is None
        assert any("fetch_excerpt raised" in m for m in caplog.messages)

    def test_haiku_raises_yields_unknown(self, monkeypatch, good_excerpt, caplog):
        _stub_fetch(monkeypatch, {"x.net": good_excerpt})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient([], raises_on={1})

        with caplog.at_level("WARNING"):
            result = sc.classify_one(record, client=client)

        assert result == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] == good_excerpt  # excerpt still preserved
        assert any("Haiku call failed" in m for m in caplog.messages)

    def test_garbage_haiku_response_yields_unknown(self, monkeypatch, good_excerpt, caplog):
        _stub_fetch(monkeypatch, {"x.net": good_excerpt})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient(["safe maybe — I'd guess legitimate"])

        with caplog.at_level("WARNING"):
            result = sc.classify_one(record, client=client)

        assert result == sc.UNKNOWN_CATEGORY
        assert any("unparseable response" in m for m in caplog.messages)


class TestClassifyOneHappyPath:
    @pytest.mark.parametrize("response,expected", [
        ("legitimate", "legitimate"),
        ("parked", "parked"),
        ("toxic", "toxic"),
        ("empty", "empty"),
        ("Legitimate.", "legitimate"),
        ("  TOXIC  ", "toxic"),
    ])
    def test_each_valid_category(self, monkeypatch, good_excerpt, response, expected):
        _stub_fetch(monkeypatch, {"x.net": good_excerpt})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient([response])

        result = sc.classify_one(record, client=client)

        assert result == expected
        assert record["snapshot_category"] == expected
        assert record["wayback_excerpt"] == good_excerpt
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_user_message_built_from_excerpt(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"x.net": good_excerpt})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient(["legitimate"])

        sc.classify_one(record, client=client)

        assert len(client.calls) == 1
        # The Haiku payload should contain the excerpt's content
        assert "Acme Roofing" in client.calls[0]


# ---------------------------------------------------------------------------
# classify_all
# ---------------------------------------------------------------------------


class TestClassifyAll:
    def test_empty_list_returns_zero_counts(self):
        counts = sc.classify_all([], client=_FakeClient([]))
        assert counts == {"legitimate": 0, "parked": 0, "toxic": 0,
                          "empty": 0, "unknown": 0}

    def test_none_client_pass_through_all_unknown(self):
        records = [
            {"name": "a.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "b.com", "wayback_last_snapshot": "2025-11-10"},
            {"name": "c.com"},
        ]
        counts = sc.classify_all(records, client=None)

        assert counts["unknown"] == 3
        assert counts["legitimate"] == 0
        for r in records:
            assert r["snapshot_category"] == sc.UNKNOWN_CATEGORY
            assert r["wayback_excerpt"] is None
            assert r["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_none_client_does_not_call_fetch(self, monkeypatch):
        # If classify_all attempts a fetch when client is None, this raises.
        def _must_not_call(name, target_date):
            raise AssertionError("no fetch when client is None")
        monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _must_not_call)

        records = [{"name": "a.com", "wayback_last_snapshot": "2025-12-15"}]
        sc.classify_all(records, client=None)

    def test_mixed_outcomes_tallied(self, monkeypatch, good_excerpt):
        # Three candidates, three outcomes.
        parked_excerpt = {**good_excerpt, "title": "example.com is for sale"}
        _stub_fetch(monkeypatch, {
            "a.com": good_excerpt,      # → legitimate
            "b.com": parked_excerpt,    # → parked
            "c.com": None,              # → unknown (fetch returned None)
        })
        records = [
            {"name": "a.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "b.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "c.com", "wayback_last_snapshot": "2025-12-15"},
        ]
        client = _FakeClient(["legitimate", "parked"])

        counts = sc.classify_all(records, client=client)

        assert counts == {"legitimate": 1, "parked": 1, "toxic": 0,
                          "empty": 0, "unknown": 1}
        assert records[0]["snapshot_category"] == "legitimate"
        assert records[1]["snapshot_category"] == "parked"
        assert records[2]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_sleep_called_between_but_not_after_last(self, monkeypatch, good_excerpt):
        # Re-install a tracking sleep AFTER the autouse no-sleep fixture.
        calls: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: calls.append(s))

        _stub_fetch(monkeypatch, {
            "a.com": good_excerpt,
            "b.com": good_excerpt,
            "c.com": good_excerpt,
        })
        records = [
            {"name": "a.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "b.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "c.com", "wayback_last_snapshot": "2025-12-15"},
        ]
        client = _FakeClient(["legitimate"] * 3)

        sc.classify_all(records, client=client, pause_seconds=0.5)

        # 3 candidates → 2 inter-candidate sleeps, none after the last
        assert calls == [0.5, 0.5]

    def test_pause_zero_skips_sleep(self, monkeypatch, good_excerpt):
        calls: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: calls.append(s))

        _stub_fetch(monkeypatch, {"a.com": good_excerpt, "b.com": good_excerpt})
        records = [
            {"name": "a.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "b.com", "wayback_last_snapshot": "2025-12-15"},
        ]
        client = _FakeClient(["legitimate", "parked"])

        sc.classify_all(records, client=client, pause_seconds=0.0)

        assert calls == []  # no sleeps at all when pause_seconds=0


# ---------------------------------------------------------------------------
# make_default_client
# ---------------------------------------------------------------------------


class TestMakeDefaultClient:
    def test_returns_none_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert sc.make_default_client() is None

    def test_returns_none_when_key_empty(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert sc.make_default_client() is None

    def test_returns_none_when_key_whitespace(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        assert sc.make_default_client() is None

    def test_builds_client_when_key_present(self, monkeypatch):
        # Inject a fake `anthropic` module via sys.modules so the deferred
        # import inside ClassifierClient.__init__ succeeds without the
        # real SDK installed (it's an OVH-only dependency).
        import sys
        import types
        fake_module = types.ModuleType("anthropic")
        fake_module.Anthropic = lambda api_key: MagicMock(api_key=api_key)
        monkeypatch.setitem(sys.modules, "anthropic", fake_module)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        client = sc.make_default_client()

        assert client is not None
        assert isinstance(client, sc.ClassifierClient)


# ---------------------------------------------------------------------------
# Version stamping
# ---------------------------------------------------------------------------


class TestVersionStamping:
    def test_version_stamped_on_no_snapshot_path(self, monkeypatch):
        _stub_fetch(monkeypatch, {})  # won't be called
        record = {"name": "noshot.net"}
        sc.classify_one(record, client=_FakeClient([]))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_fetch_failure(self, monkeypatch):
        _stub_fetch(monkeypatch, {"x.net": None})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        sc.classify_one(record, client=_FakeClient([]))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_haiku_failure(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"x.net": good_excerpt})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        sc.classify_one(record, client=_FakeClient([], raises_on={1}))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_successful_classification(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"x.net": good_excerpt})
        record = {"name": "x.net", "wayback_last_snapshot": "2025-12-15"}
        sc.classify_one(record, client=_FakeClient(["legitimate"]))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_no_client_pass_through(self):
        records = [{"name": "a.com"}, {"name": "b.com"}]
        sc.classify_all(records, client=None)
        for r in records:
            assert r["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION
