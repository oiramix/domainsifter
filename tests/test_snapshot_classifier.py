"""Unit tests for scripts/snapshot_classifier.py.

All model + Wayback calls are mocked: no network, no subprocess, no API key.
The classifier has no module-level mutable state, so tests are
order-independent. Every domain used here is invented (hard rule 1).

Coverage:
  - cfg() defaults + config override
  - _parse_classification: case / punctuation / non-string / out-of-vocab
  - _excerpt_fields + _build_user_message: content fields only, no metadata
  - _chunked / batch splitting: N domains at batch_size B → ceil(N/B) calls
  - Partial reply → the omitted domains become unknown
  - One batch raising → that batch unknown, the other batches unaffected
  - Refusal / prose / empty-array reply → unknown, never an exception
  - Hallucinated domain in the reply → ignored, never applied to a record
  - Shadow mode ON  → snapshot_category_shadow written, snapshot_category
    left alone (and an existing label is NOT clobbered)
  - Shadow mode OFF → snapshot_category written for real
  - Fetch failure paths (no snapshot date / None / raises) → unknown
  - client=None pass-through → all unknown, no fetch attempted
  - snapshot_classifier_version stamped on every record the classifier touched
  - The canonical summary line is emitted in BOTH modes, shadow line only
    in shadow mode
  - classify_all fetch pacing (only between fetches, not after the last)

2026-09-21 additions:
  - excerpt_cache reuse: a usable cached excerpt SKIPS the network entirely
    and never sleeps; a cached None is a remembered failure and IS retried;
    a failed re-fetch NEVER replaces a usable cached excerpt (the invariant
    the whole change exists for); age-based re-fetch; an unparseable
    snapshot_timestamp means "reuse", not "crash" and not "re-fetch
    everything"; reuse_cached_excerpts=false restores the old behaviour
  - parked_markers: a marker in the RAW snapshot HTML sets "parked" with no
    model call, shadow-aware, and every production marker matches
    case-insensitively
  - spam_signatures wiring: a signature firing on a domain the model did not
    call toxic applies signature_action and is logged by name
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import snapshot_classifier as sc
from scripts.llm_backend import LLMBackendError

# Shorthand configs. Shadow defaults to TRUE in-code, so tests that want the
# real field written must say so explicitly.
SHADOW_OFF: dict = {"snapshot_classifier": {"shadow": False}}
SHADOW_ON: dict = {"snapshot_classifier": {"shadow": True}}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _names_in(user_message: str) -> list[str]:
    """Extract the domain list from a rendered batch user message."""
    start, end = user_message.find("["), user_message.rfind("]")
    return [row["domain"] for row in json.loads(user_message[start : end + 1])]


class _FakeClient:
    """Stand-in for BatchClassifierClient.

    `handler` is either a list of scripted replies (str, or an Exception
    instance to raise) consumed one per batch, or a callable taking the list
    of domain names in the batch and returning the reply string.

    Records every batch it saw in `self.batches`.
    """

    def __init__(self, handler):
        self._handler = handler if callable(handler) else list(handler)
        self.batches: list[list[str]] = []
        self.timeouts: list[int | None] = []

    def classify_batch(self, user: str, *, timeout_seconds: int | None = None) -> str:
        names = _names_in(user)
        self.batches.append(names)
        self.timeouts.append(timeout_seconds)
        if callable(self._handler):
            return self._handler(names)
        if not self._handler:
            return _reply({n: "legitimate" for n in names})
        nxt = self._handler.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _reply(mapping: dict[str, str]) -> str:
    """Render a well-formed model reply from domain → category."""
    return json.dumps(
        [{"domain": d, "category": c} for d, c in mapping.items()],
        ensure_ascii=False,
    )


def _all(category: str):
    """Handler: label every domain in the batch with `category`."""
    return lambda names: _reply({n: category for n in names})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Globally stub out time.sleep — the per-fetch pause adds nothing
    to tests but wall-clock cost."""
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)


@pytest.fixture
def good_excerpt():
    """A minimal-but-realistic excerpt with all four fields populated."""
    return {
        "snapshot_timestamp": "20251215120000",
        "snapshot_url": "http://web.archive.org/web/20251215120000/http://coppernest.org/",
        "title": "Coppernest Roofing — Boston MA",
        "meta_description": "Family-owned roofers since 1985.",
        "h1": ["Coppernest Roofing"],
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


def _records(*names: str) -> list[dict]:
    return [
        {"name": n, "wayback_last_snapshot": "2025-12-15"} for n in names
    ]


# ---------------------------------------------------------------------------
# cfg
# ---------------------------------------------------------------------------


class TestCfg:
    def test_defaults_when_section_absent(self):
        assert sc.cfg({}, "batch_size") == 20
        assert sc.cfg({}, "shadow") is True
        assert sc.cfg({}, "timeout_seconds") is None

    def test_defaults_when_config_is_none(self):
        assert sc.cfg(None, "batch_size") == 20
        assert sc.cfg(None, "shadow") is True

    def test_config_overrides_default(self):
        config = {"snapshot_classifier": {"batch_size": 5, "shadow": False}}
        assert sc.cfg(config, "batch_size") == 5
        assert sc.cfg(config, "shadow") is False

    def test_missing_key_inside_present_section_falls_back(self):
        config = {"snapshot_classifier": {"batch_size": 5}}
        assert sc.cfg(config, "shadow") is True

    def test_malformed_section_falls_back(self):
        assert sc.cfg({"snapshot_classifier": "nope"}, "batch_size") == 20


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
        assert sc._parse_classification("this is legitimate") == sc.UNKNOWN_CATEGORY

    def test_non_string_returns_unknown(self):
        # The model can emit null, a number, or a nested object for
        # "category" — none of them may crash the parser.
        assert sc._parse_classification(None) == sc.UNKNOWN_CATEGORY
        assert sc._parse_classification(7) == sc.UNKNOWN_CATEGORY
        assert sc._parse_classification({"category": "toxic"}) == sc.UNKNOWN_CATEGORY


# ---------------------------------------------------------------------------
# _excerpt_fields / _build_user_message
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    def test_only_content_fields_included(self, good_excerpt):
        msg = sc._build_user_message([("coppernest.org", good_excerpt)])
        assert "snapshot_timestamp" not in msg
        assert "snapshot_url" not in msg
        assert "Coppernest Roofing" in msg
        for field in ("domain", "title", "meta_description", "h1", "h2"):
            assert field in msg

    def test_domain_echoed_for_every_batch_member(self, good_excerpt):
        batch = [("marketglow.com", good_excerpt), ("tideblock.io", good_excerpt)]
        msg = sc._build_user_message(batch)
        assert _names_in(msg) == ["marketglow.com", "tideblock.io"]

    def test_non_latin_preserved_as_chars_not_escapes(self):
        excerpt = {"title": "月見うどん専門店", "meta_description": None,
                   "h1": [], "h2": []}
        msg = sc._build_user_message([("tsukimi-udon.store", excerpt)])
        assert "月見" in msg
        assert "\\u" not in msg

    def test_missing_lists_default_to_empty(self):
        excerpt = {"title": "X", "meta_description": "Y"}  # no h1/h2 keys
        msg = sc._build_user_message([("tideblock.io", excerpt)])
        assert '"h1":[]' in msg
        assert '"h2":[]' in msg

    def test_payload_is_compact_json(self, good_excerpt):
        msg = sc._build_user_message([("coppernest.org", good_excerpt)])
        body = msg[msg.find("[") : msg.rfind("]") + 1]
        assert "\n" not in body
        assert ", " not in body


# ---------------------------------------------------------------------------
# _chunked
# ---------------------------------------------------------------------------


class TestChunked:
    def test_exact_multiple(self):
        assert sc._chunked([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

    def test_remainder_becomes_short_final_chunk(self):
        assert sc._chunked([1, 2, 3], 2) == [[1, 2], [3]]

    def test_empty_input(self):
        assert sc._chunked([], 5) == []

    def test_size_below_one_coerced_to_one(self):
        assert sc._chunked([1, 2], 0) == [[1], [2]]


# ---------------------------------------------------------------------------
# _parse_batch_reply
# ---------------------------------------------------------------------------


class TestParseBatchReply:
    def test_maps_domains_to_categories(self):
        text = _reply({"marketglow.com": "parked", "tideblock.io": "legitimate"})
        out = sc._parse_batch_reply(text, ["marketglow.com", "tideblock.io"])
        assert out == {"marketglow.com": "parked", "tideblock.io": "legitimate"}

    def test_case_insensitive_domain_match(self):
        text = _reply({"MarketGlow.com": "toxic"})
        assert sc._parse_batch_reply(text, ["marketglow.com"]) == {
            "marketglow.com": "toxic"
        }

    def test_unrequested_domain_ignored(self, caplog):
        text = _reply({"marketglow.com": "parked", "neverasked.dev": "toxic"})
        with caplog.at_level("WARNING"):
            out = sc._parse_batch_reply(text, ["marketglow.com"])
        assert out == {"marketglow.com": "parked"}
        assert any("unrequested domain" in m for m in caplog.messages)

    def test_invented_category_coerced_to_unknown(self):
        text = _reply({"marketglow.com": "suspicious"})
        assert sc._parse_batch_reply(text, ["marketglow.com"]) == {
            "marketglow.com": sc.UNKNOWN_CATEGORY
        }

    def test_markdown_fence_tolerated(self):
        text = "```json\n" + _reply({"tideblock.io": "empty"}) + "\n```"
        assert sc._parse_batch_reply(text, ["tideblock.io"]) == {
            "tideblock.io": "empty"
        }

    def test_prose_raises_backend_error(self):
        with pytest.raises(LLMBackendError):
            sc._parse_batch_reply(
                "I'm not able to help with classifying that content.",
                ["marketglow.com"],
            )

    def test_missing_domain_simply_absent(self):
        text = _reply({"marketglow.com": "parked"})
        out = sc._parse_batch_reply(text, ["marketglow.com", "tideblock.io"])
        assert "tideblock.io" not in out


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


class TestBatching:
    def test_splits_into_batches_of_configured_size(self, monkeypatch, good_excerpt):
        names = [f"lot{i:02d}test.dev" for i in range(43)]
        _stub_fetch(monkeypatch, {n: good_excerpt for n in names})
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(
            _records(*names), client=client, pause_seconds=0.0,
            config={"snapshot_classifier": {"batch_size": 20, "shadow": False}},
        )

        assert [len(b) for b in client.batches] == [20, 20, 3]
        # Every domain sent exactly once, order preserved.
        assert [n for b in client.batches for n in b] == names

    def test_default_batch_size_is_twenty(self, monkeypatch, good_excerpt):
        names = [f"lot{i:02d}test.dev" for i in range(21)]
        _stub_fetch(monkeypatch, {n: good_excerpt for n in names})
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(_records(*names), client=client, pause_seconds=0.0)

        assert [len(b) for b in client.batches] == [20, 1]

    def test_single_call_when_batch_larger_than_input(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt, "tideblock.io": good_excerpt,
        })
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(
            _records("marketglow.com", "tideblock.io"),
            client=client, pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert len(client.batches) == 1

    def test_records_without_excerpt_are_not_batched(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt,
            "tideblock.io": None,             # fetch miss
        })
        records = _records("marketglow.com", "tideblock.io")
        records.append({"name": "nosnapshot.dev"})  # no snapshot date at all
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert client.batches == [["marketglow.com"]]
        assert records[1]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert records[2]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_timeout_forwarded_from_config(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(
            _records("marketglow.com"), client=client, pause_seconds=0.0,
            config={"snapshot_classifier": {"timeout_seconds": 120}},
        )

        assert client.timeouts == [120]

    def test_timeout_none_by_default(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(_records("marketglow.com"), client=client, pause_seconds=0.0)

        assert client.timeouts == [None]


# ---------------------------------------------------------------------------
# Batch failure containment (hard rules 11 / 17)
# ---------------------------------------------------------------------------


class TestBatchFailureContainment:
    def test_partial_reply_marks_missing_domains_unknown(
        self, monkeypatch, good_excerpt, caplog,
    ):
        names = ["marketglow.com", "tideblock.io", "coppernest.org"]
        _stub_fetch(monkeypatch, {n: good_excerpt for n in names})
        # Model answers for 2 of 3.
        client = _FakeClient([_reply({
            "marketglow.com": "parked", "coppernest.org": "legitimate",
        })])
        records = _records(*names)

        with caplog.at_level("WARNING"):
            counts = sc.classify_all(
                records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
            )

        assert records[0]["snapshot_category"] == "parked"
        assert records[1]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert records[2]["snapshot_category"] == "legitimate"
        assert counts["unknown"] == 1
        assert any("omitted 1/3 domains" in m for m in caplog.messages)

    def test_failed_batch_does_not_poison_other_batches(
        self, monkeypatch, good_excerpt, caplog,
    ):
        names = [f"lot{i:02d}test.dev" for i in range(4)]
        _stub_fetch(monkeypatch, {n: good_excerpt for n in names})
        # batch_size=2 → two batches; the FIRST one blows up.
        client = _FakeClient([
            LLMBackendError("claude -p exited 1: auth failure"),
            _reply({names[2]: "toxic", names[3]: "parked"}),
        ])
        records = _records(*names)

        with caplog.at_level("WARNING"):
            counts = sc.classify_all(
                records, client=client, pause_seconds=0.0,
                config={"snapshot_classifier": {"batch_size": 2, "shadow": False}},
            )

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert records[1]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert records[2]["snapshot_category"] == "toxic"
        assert records[3]["snapshot_category"] == "parked"
        assert counts == {"legitimate": 0, "parked": 1, "toxic": 1,
                          "empty": 0, "unknown": 2}
        assert any("batch of 2 failed" in m for m in caplog.messages)

    def test_unexpected_exception_type_also_contained(
        self, monkeypatch, good_excerpt,
    ):
        # A backend that raises something other than LLMBackendError (a bug,
        # a socket error escaping the wrapper) must still fail soft.
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([RuntimeError("boom")])
        records = _records("marketglow.com")

        counts = sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert counts["unknown"] == 1

    def test_refusal_prose_yields_unknown_not_crash(
        self, monkeypatch, good_excerpt, caplog,
    ):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([
            "I'm sorry, but I can't help with evaluating adult content.",
        ])
        records = _records("marketglow.com")

        with caplog.at_level("WARNING"):
            counts = sc.classify_all(
                records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
            )

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert counts["unknown"] == 1
        assert any("unparseable batch reply" in m for m in caplog.messages)

    def test_empty_json_array_reply_yields_unknown(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient(["[]"])
        records = _records("marketglow.com")

        sc.classify_all(records, client=client, pause_seconds=0.0, config=SHADOW_OFF)

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_hallucinated_domain_not_applied_to_any_record(
        self, monkeypatch, good_excerpt,
    ):
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt, "tideblock.io": good_excerpt,
        })
        # The model answers about a domain that was never in the batch.
        client = _FakeClient([_reply({
            "marketglow.com": "legitimate", "ghostwritten.dev": "toxic",
        })])
        records = _records("marketglow.com", "tideblock.io")

        sc.classify_all(records, client=client, pause_seconds=0.0, config=SHADOW_OFF)

        assert records[0]["snapshot_category"] == "legitimate"
        assert records[1]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_unknown_category_from_model_coerced(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([_reply({"marketglow.com": "spammy"})])
        records = _records("marketglow.com")

        counts = sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert counts["unknown"] == 1


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------


class TestShadowMode:
    def test_shadow_is_the_default(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([_reply({"marketglow.com": "toxic"})])
        records = _records("marketglow.com")

        sc.classify_all(records, client=client, pause_seconds=0.0)  # no config

        assert records[0][sc.SHADOW_FIELD] == "toxic"
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_shadow_writes_shadow_field_only(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt, "tideblock.io": good_excerpt,
        })
        client = _FakeClient([_reply({
            "marketglow.com": "toxic", "tideblock.io": "parked",
        })])
        records = _records("marketglow.com", "tideblock.io")

        counts = sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_ON,
        )

        assert records[0][sc.SHADOW_FIELD] == "toxic"
        assert records[1][sc.SHADOW_FIELD] == "parked"
        for r in records:
            assert r["snapshot_category"] == sc.UNKNOWN_CATEGORY
        # Effective counts are what filter.py will act on: nothing.
        assert counts == {"legitimate": 0, "parked": 0, "toxic": 0,
                          "empty": 0, "unknown": 2}

    def test_shadow_does_not_clobber_an_existing_label(
        self, monkeypatch, good_excerpt,
    ):
        # A --force re-run under shadow must not blank out a label the
        # API-era classifier already earned.
        _stub_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        client = _FakeClient([_reply({"coppernest.org": "parked"})])
        record = {
            "name": "coppernest.org",
            "wayback_last_snapshot": "2025-12-15",
            "snapshot_category": "legitimate",
        }

        sc.classify_all([record], client=client, pause_seconds=0.0, config=SHADOW_ON)

        assert record["snapshot_category"] == "legitimate"
        assert record[sc.SHADOW_FIELD] == "parked"

    def test_non_shadow_writes_real_field(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt, "tideblock.io": good_excerpt,
        })
        client = _FakeClient([_reply({
            "marketglow.com": "toxic", "tideblock.io": "parked",
        })])
        records = _records("marketglow.com", "tideblock.io")

        counts = sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert records[0]["snapshot_category"] == "toxic"
        assert records[1]["snapshot_category"] == "parked"
        assert sc.SHADOW_FIELD not in records[0]
        assert counts["toxic"] == 1 and counts["parked"] == 1

    def test_non_shadow_overwrites_previous_label(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        client = _FakeClient([_reply({"coppernest.org": "toxic"})])
        record = {
            "name": "coppernest.org",
            "wayback_last_snapshot": "2025-12-15",
            "snapshot_category": "legitimate",
        }

        sc.classify_all([record], client=client, pause_seconds=0.0, config=SHADOW_OFF)

        assert record["snapshot_category"] == "toxic"

    def test_shadow_records_unknown_for_failed_batch(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([LLMBackendError("down")])
        records = _records("marketglow.com")

        sc.classify_all(records, client=client, pause_seconds=0.0, config=SHADOW_ON)

        assert records[0][sc.SHADOW_FIELD] == sc.UNKNOWN_CATEGORY
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY


# ---------------------------------------------------------------------------
# Summary logging (send_report.py parses the canonical line)
# ---------------------------------------------------------------------------


class TestSummaryLogging:
    def test_canonical_line_emitted_in_shadow_mode(
        self, monkeypatch, good_excerpt, caplog,
    ):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([_reply({"marketglow.com": "toxic"})])

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records("marketglow.com"), client=client,
                pause_seconds=0.0, config=SHADOW_ON,
            )

        assert any(
            m == ("snapshot_classifier: results — 0 legitimate, 0 parked, "
                  "0 toxic, 0 empty, 1 unknown")
            for m in caplog.messages
        )
        assert any("SHADOW verdicts" in m and "1 toxic" in m for m in caplog.messages)

    def test_canonical_line_emitted_in_live_mode(
        self, monkeypatch, good_excerpt, caplog,
    ):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([_reply({"marketglow.com": "toxic"})])

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records("marketglow.com"), client=client,
                pause_seconds=0.0, config=SHADOW_OFF,
            )

        assert any(
            m == ("snapshot_classifier: results — 0 legitimate, 0 parked, "
                  "1 toxic, 0 empty, 0 unknown")
            for m in caplog.messages
        )
        assert not any("SHADOW verdicts" in m for m in caplog.messages)

    def test_shadow_names_the_domains_it_would_evict(
        self, monkeypatch, good_excerpt, caplog,
    ):
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt, "tideblock.io": good_excerpt,
        })
        client = _FakeClient([_reply({
            "marketglow.com": "toxic", "tideblock.io": "legitimate",
        })])

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records("marketglow.com", "tideblock.io"), client=client,
                pause_seconds=0.0, config=SHADOW_ON,
            )

        assert any(
            "SHADOW would evict 1 as toxic — marketglow.com" in m
            for m in caplog.messages
        )

    def test_no_eviction_line_when_nothing_toxic(
        self, monkeypatch, good_excerpt, caplog,
    ):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient(_all("legitimate"))

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records("marketglow.com"), client=client,
                pause_seconds=0.0, config=SHADOW_ON,
            )

        assert not any("would evict" in m for m in caplog.messages)

    def test_canonical_line_is_last(self, monkeypatch, good_excerpt, caplog):
        # A log consumer taking the LAST match must get the effective numbers.
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        client = _FakeClient([_reply({"marketglow.com": "toxic"})])

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records("marketglow.com"), client=client,
                pause_seconds=0.0, config=SHADOW_ON,
            )

        assert caplog.messages[-1].startswith("snapshot_classifier: results — ")


# ---------------------------------------------------------------------------
# Fetch failure paths — every one lands on UNKNOWN_CATEGORY
# ---------------------------------------------------------------------------


class TestFetchFailurePaths:
    def test_no_wayback_last_snapshot_skips_fetch(self, monkeypatch):
        def _must_not_call(name, target_date):
            raise AssertionError("fetch_excerpt must not run without a snapshot date")
        monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _must_not_call)

        record = {"name": "nosnapshot.dev"}
        client = _FakeClient(_all("legitimate"))
        result = sc.classify_one(record, client=client, config=SHADOW_OFF)

        assert result == sc.UNKNOWN_CATEGORY
        assert record["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] is None
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION
        assert client.batches == []  # no model call either

    def test_fetch_returns_none_yields_unknown(self, monkeypatch):
        _stub_fetch(monkeypatch, {"nosnapshot.dev": None})
        record = {"name": "nosnapshot.dev", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient(_all("legitimate"))
        result = sc.classify_one(record, client=client, config=SHADOW_OFF)

        assert result == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] is None
        assert client.batches == []  # no model call when there's no excerpt

    def test_fetch_raises_yields_unknown(self, monkeypatch, caplog):
        def _boom(name, target_date):
            raise RuntimeError("bs4 internal error")
        _stub_fetch(monkeypatch, {"brokenfetch.dev": _boom})

        record = {"name": "brokenfetch.dev", "wayback_last_snapshot": "2025-12-15"}
        with caplog.at_level("WARNING"):
            result = sc.classify_one(
                record, client=_FakeClient(_all("legitimate")), config=SHADOW_OFF,
            )

        assert result == sc.UNKNOWN_CATEGORY
        assert record["wayback_excerpt"] is None
        assert any("fetch_excerpt raised" in m for m in caplog.messages)

    def test_excerpt_preserved_even_when_model_fails(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}

        sc.classify_one(
            record, client=_FakeClient([LLMBackendError("down")]), config=SHADOW_OFF,
        )

        assert record["wayback_excerpt"] == good_excerpt
        assert record["snapshot_category"] == sc.UNKNOWN_CATEGORY


# ---------------------------------------------------------------------------
# classify_one happy path
# ---------------------------------------------------------------------------


class TestClassifyOne:
    @pytest.mark.parametrize("category", ["legitimate", "parked", "toxic", "empty"])
    def test_each_valid_category(self, monkeypatch, good_excerpt, category):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient([_reply({"marketglow.com": category})])

        result = sc.classify_one(record, client=client, config=SHADOW_OFF)

        assert result == category
        assert record["snapshot_category"] == category
        assert record["wayback_excerpt"] == good_excerpt
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_user_message_built_from_excerpt(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient(_all("legitimate"))

        sc.classify_one(record, client=client, config=SHADOW_OFF)

        assert client.batches == [["marketglow.com"]]

    def test_returns_effective_category_under_shadow(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}
        client = _FakeClient([_reply({"marketglow.com": "toxic"})])

        result = sc.classify_one(record, client=client, config=SHADOW_ON)

        assert result == sc.UNKNOWN_CATEGORY
        assert record[sc.SHADOW_FIELD] == "toxic"


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
            {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"},
            {"name": "tideblock.io", "wayback_last_snapshot": "2025-11-10"},
            {"name": "coppernest.org"},
        ]
        counts = sc.classify_all(records, client=None, config=SHADOW_OFF)

        assert counts["unknown"] == 3
        assert counts["legitimate"] == 0
        for r in records:
            assert r["snapshot_category"] == sc.UNKNOWN_CATEGORY
            assert r["wayback_excerpt"] is None
            assert r["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_none_client_does_not_call_fetch(self, monkeypatch):
        def _must_not_call(name, target_date):
            raise AssertionError("no fetch when client is None")
        monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _must_not_call)

        sc.classify_all(_records("marketglow.com"), client=None)

    def test_mixed_outcomes_tallied(self, monkeypatch, good_excerpt):
        parked_excerpt = {**good_excerpt, "title": "marketglow.com is for sale"}
        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt,     # → legitimate
            "tideblock.io": parked_excerpt,     # → parked
            "coppernest.org": None,             # → unknown (fetch returned None)
        })
        records = _records("marketglow.com", "tideblock.io", "coppernest.org")
        client = _FakeClient([_reply({
            "marketglow.com": "legitimate", "tideblock.io": "parked",
        })])

        counts = sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert counts == {"legitimate": 1, "parked": 1, "toxic": 0,
                          "empty": 0, "unknown": 1}
        assert records[0]["snapshot_category"] == "legitimate"
        assert records[1]["snapshot_category"] == "parked"
        assert records[2]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_sleep_called_between_fetches_but_not_after_last(
        self, monkeypatch, good_excerpt,
    ):
        calls: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: calls.append(s))

        names = ["marketglow.com", "tideblock.io", "coppernest.org"]
        _stub_fetch(monkeypatch, {n: good_excerpt for n in names})
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(_records(*names), client=client, pause_seconds=0.5)

        # 3 fetches → 2 inter-fetch sleeps, none after the last
        assert calls == [0.5, 0.5]

    def test_no_sleep_for_records_without_snapshot_date(
        self, monkeypatch, good_excerpt,
    ):
        calls: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: calls.append(s))
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})

        records = _records("marketglow.com")
        records.append({"name": "nosnapshot.dev"})
        sc.classify_all(records, client=_FakeClient(_all("legitimate")),
                        pause_seconds=0.5)

        assert calls == []  # only one fetch happened → no inter-fetch pause

    def test_pause_zero_skips_sleep(self, monkeypatch, good_excerpt):
        calls: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: calls.append(s))

        _stub_fetch(monkeypatch, {
            "marketglow.com": good_excerpt, "tideblock.io": good_excerpt,
        })
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(
            _records("marketglow.com", "tideblock.io"),
            client=client, pause_seconds=0.0,
        )

        assert calls == []  # no sleeps at all when pause_seconds=0


# ---------------------------------------------------------------------------
# make_default_client
# ---------------------------------------------------------------------------


class TestMakeDefaultClient:
    def test_builds_claude_code_backend_from_config(self):
        client = sc.make_default_client({"llm": {"backend": "claude_code"}})
        assert isinstance(client, sc.BatchClassifierClient)
        assert client.backend_name == "claude_code"

    def test_builds_api_backend_from_config(self):
        client = sc.make_default_client({"llm": {"backend": "api"}})
        assert isinstance(client, sc.BatchClassifierClient)
        assert client.backend_name == "api"

    def test_no_config_uses_backend_defaults(self):
        # Zero-argument call kept working for pre-existing callers.
        client = sc.make_default_client()
        assert isinstance(client, sc.BatchClassifierClient)

    def test_unknown_backend_returns_none(self, caplog):
        with caplog.at_level("ERROR"):
            client = sc.make_default_client({"llm": {"backend": "gpt5"}})
        assert client is None
        assert any("no usable LLM backend" in m for m in caplog.messages)

    def test_client_forwards_system_prompt_and_timeout(self):
        seen: dict = {}

        class _Backend:
            name = "fake"
            def complete(self, *, system, user, timeout_seconds=None):
                seen.update(system=system, user=user, timeout=timeout_seconds)
                return "[]"

        client = sc.BatchClassifierClient(_Backend())
        client.classify_batch("payload", timeout_seconds=42)

        assert seen["system"] == sc.SNAPSHOT_CLASSIFIER_SYSTEM_PROMPT
        assert seen["user"] == "payload"
        assert seen["timeout"] == 42


# ---------------------------------------------------------------------------
# Version stamping
# ---------------------------------------------------------------------------


class TestVersionStamping:
    def test_version_stamped_on_no_snapshot_path(self, monkeypatch):
        _stub_fetch(monkeypatch, {})  # won't be called
        record = {"name": "nosnapshot.dev"}
        sc.classify_one(record, client=_FakeClient(_all("legitimate")))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_fetch_failure(self, monkeypatch):
        _stub_fetch(monkeypatch, {"marketglow.com": None})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}
        sc.classify_one(record, client=_FakeClient(_all("legitimate")))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_model_failure(self, monkeypatch, good_excerpt):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}
        sc.classify_one(record, client=_FakeClient([LLMBackendError("down")]))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_successful_classification(
        self, monkeypatch, good_excerpt,
    ):
        _stub_fetch(monkeypatch, {"marketglow.com": good_excerpt})
        record = {"name": "marketglow.com", "wayback_last_snapshot": "2025-12-15"}
        sc.classify_one(record, client=_FakeClient(_all("legitimate")))
        assert record["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

    def test_version_stamped_on_no_client_pass_through(self):
        records = [{"name": "marketglow.com"}, {"name": "tideblock.io"}]
        sc.classify_all(records, client=None)
        for r in records:
            assert r["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION


# ===========================================================================
# 2026-09-21 — excerpt reuse, parked-marker detection, spam signatures
# ===========================================================================

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "scripts" / "config.json"

# A parked page as it actually appears: a couple of KB of ad-network markup
# with no title, no meta description and no headings, so wayback_excerpt
# extracts NO content signal at all and the old code path could only file it
# under "unknown".
PARKED_HTML = (
    "<!DOCTYPE html><html><head>"
    "<script src=\"https://ww1.example-ads.test/px.js\"></script>"
    "</head><body data-adblockkey=\"MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJB\">"
    "<div id=\"target\"></div></body></html>"
)

REAL_SITE_HTML = (
    "<!DOCTYPE html><html><head><title>Coppernest Woodworking Journal</title>"
    "<meta name=\"description\" content=\"Hand-tool joinery notes.\">"
    "</head><body><h1>Hand-tool joinery</h1><h2>Recent projects</h2></body></html>"
)

SIGNATURE_CONFIG: dict = {
    "snapshot_classifier": {
        "shadow": False,
        "signature_action": "toxic",
        "signature_terms_latin": ["casino"],
        "signature_terms_cjk": ["金沙"],
    }
}


def _ts(days_ago: float) -> str:
    """A 14-digit Wayback timestamp `days_ago` days in the past."""
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y%m%d%H%M%S")


def _cached(title: str = "Tideblock Tidal Data", *, age_days: float = 1.0) -> dict:
    """A usable cached excerpt with a controllable capture age."""
    return {
        "snapshot_timestamp": _ts(age_days),
        "snapshot_url": "http://web.archive.org/web/x/http://tideblock.io/",
        "title": title,
        "meta_description": None,
        "h1": [],
        "h2": [],
    }


def _spy_fetch(monkeypatch, by_name: dict) -> list[str]:
    """Stub fetch_excerpt and RECORD every domain it was called with.

    A recording spy rather than an exception-raising stub because
    _fetch_excerpt_only deliberately swallows exceptions (hard rule 11) — an
    AssertionError raised in there would be logged, not surfaced.
    """
    calls: list[str] = []

    def _stub(name, target_date):
        calls.append(name)
        value = by_name.get(name)
        if callable(value):
            return value(name, target_date)
        return value

    monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _stub)
    return calls


def _stub_bundle(monkeypatch, html_by_name: dict) -> list[str]:
    """Stub the three network helpers the raw-markup path composes.

    `_parse_content_signals` is left REAL, so these tests exercise the actual
    bs4 extraction over the fixture markup and prove the assembled excerpt has
    fetch_excerpt's exact shape. Returns the list of fetched domains.
    """
    from scripts import wayback_excerpt as we

    calls: list[str] = []

    def _availability(domain, target_date):
        calls.append(domain)
        if domain not in html_by_name:
            return None
        return {"domain": domain}

    def _closest(payload):
        if not payload:
            return None
        return {
            "available": True,
            "timestamp": "20260901120000",
            "url": (
                "http://web.archive.org/web/20260901120000/"
                f"http://{payload['domain']}/"
            ),
        }

    def _html(url):
        name = url.rstrip("/").rsplit("/", 1)[-1]
        value = html_by_name.get(name)
        if isinstance(value, str):
            return value.encode("utf-8")
        return value

    monkeypatch.setattr(we, "_fetch_availability", _availability)
    monkeypatch.setattr(we, "_extract_closest_snapshot", _closest)
    monkeypatch.setattr(we, "_fetch_snapshot_html", _html)
    return calls


def _markers_config(markers: list[str], **extra) -> dict:
    section = {"shadow": False, "parked_markers": markers}
    section.update(extra)
    return {"snapshot_classifier": section}


def _production_markers() -> list[str]:
    """The live parked_markers from scripts/config.json, for parametrisation.

    Returns [] rather than skipping when the key is missing: this runs at
    COLLECTION time inside a parametrize decorator, where pytest.skip is not
    allowed. An empty parameter set skips the test on its own.
    """
    if not CONFIG_PATH.exists():  # pragma: no cover - config is checked in
        return []
    section = (
        json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("snapshot_classifier")
        or {}
    )
    markers = section.get("parked_markers")
    if not isinstance(markers, list):
        return []
    return [m for m in markers if isinstance(m, str) and m.strip()]


# ---------------------------------------------------------------------------
# is_usable_excerpt / age helpers
# ---------------------------------------------------------------------------


class TestUsableExcerpt:
    @pytest.mark.parametrize("field,value", [
        ("title", "T"), ("meta_description", "M"),
        ("h1", ["A"]), ("h2", ["B"]),
    ])
    def test_any_single_signal_is_usable(self, field, value):
        assert sc.is_usable_excerpt({field: value}) is True

    def test_all_fields_empty_is_not_usable(self):
        assert sc.is_usable_excerpt(
            {"title": None, "meta_description": "", "h1": [], "h2": []}
        ) is False

    def test_none_and_non_dict_not_usable(self):
        assert sc.is_usable_excerpt(None) is False
        assert sc.is_usable_excerpt("title") is False
        assert sc.is_usable_excerpt([{"title": "T"}]) is False


class TestExcerptAge:
    def test_age_in_days_from_wayback_timestamp(self):
        age = sc._excerpt_age_days({"snapshot_timestamp": _ts(10)})
        assert age is not None and 9.9 < age < 10.1

    @pytest.mark.parametrize("stamp", [
        None, "", "2025-12-15", "2025121512", "20251215120000000",
        "not-a-timestamp", "99999999999999", 20251215120000,
    ])
    def test_unparseable_timestamp_is_unknowable_not_a_crash(self, stamp):
        assert sc._excerpt_age_days({"snapshot_timestamp": stamp}) is None

    def test_missing_key_is_unknowable(self):
        assert sc._excerpt_age_days({}) is None

    def test_unknowable_age_is_never_stale(self):
        # "I cannot tell how old this is" must mean REUSE — never re-fetch
        # everything, and never raise.
        assert sc._is_stale({"snapshot_timestamp": "nonsense"}, 90) is False

    def test_stale_beyond_max_age(self):
        assert sc._is_stale({"snapshot_timestamp": _ts(120)}, 90) is True

    def test_fresh_within_max_age(self):
        assert sc._is_stale({"snapshot_timestamp": _ts(30)}, 90) is False

    def test_zero_max_age_disables_ageing(self):
        assert sc._is_stale({"snapshot_timestamp": _ts(4000)}, 0) is False

    def test_non_numeric_max_age_disables_ageing(self, caplog):
        config = {"snapshot_classifier": {"excerpt_max_age_days": "ninety"}}
        with caplog.at_level("WARNING"):
            assert sc._max_excerpt_age_days(config) == 0.0
        assert any("excerpt_max_age_days" in m for m in caplog.messages)

    def test_default_max_age_is_ninety(self):
        assert sc._max_excerpt_age_days({}) == 90.0


# ---------------------------------------------------------------------------
# Excerpt reuse — the fix for verdict instability
# ---------------------------------------------------------------------------


class TestExcerptReuse:
    def test_usable_cached_excerpt_skips_the_network(self, monkeypatch):
        calls = _spy_fetch(monkeypatch, {})
        cached = _cached()
        records = _records("tideblock.io")
        client = _FakeClient(_all("legitimate"))

        sc.classify_all(
            records, client=client, pause_seconds=0.0, config=SHADOW_OFF,
            excerpt_cache={"tideblock.io": cached},
        )

        assert calls == []                               # no archive.org call
        assert records[0]["wayback_excerpt"] == cached    # same evidence as before
        assert records[0]["snapshot_category"] == "legitimate"
        assert client.batches == [["tideblock.io"]]       # still classified

    def test_reuse_count_logged_once_not_per_domain(self, monkeypatch, caplog):
        names = ["tideblock.io", "marketglow.com", "coppernest.org"]
        _spy_fetch(monkeypatch, {})
        cache = {n: _cached() for n in names}

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records(*names), client=_FakeClient(_all("legitimate")),
                pause_seconds=0.0, config=SHADOW_OFF, excerpt_cache=cache,
            )

        summaries = [m for m in caplog.messages if "evidence —" in m]
        assert len(summaries) == 1
        assert "3 excerpt(s) reused from cache" in summaries[0]
        assert "0 fetched from archive.org" in summaries[0]

    def test_cached_none_is_a_remembered_failure_and_is_retried(
        self, monkeypatch, good_excerpt,
    ):
        # Retrying failures is the only way coverage improves — a remembered
        # failure must never become permanent.
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})
        records = _records("tideblock.io")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=SHADOW_OFF, excerpt_cache={"tideblock.io": None},
        )

        assert calls == ["tideblock.io"]
        assert records[0]["wayback_excerpt"] == good_excerpt
        assert records[0]["snapshot_category"] == "legitimate"

    def test_unusable_cached_dict_is_retried(self, monkeypatch, good_excerpt):
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})
        empty_cached = {"snapshot_timestamp": _ts(1), "title": None,
                        "meta_description": None, "h1": [], "h2": []}

        sc.classify_all(
            _records("tideblock.io"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0, config=SHADOW_OFF,
            excerpt_cache={"tideblock.io": empty_cached},
        )

        assert calls == ["tideblock.io"]

    def test_cache_miss_falls_through_to_fetch(self, monkeypatch, good_excerpt):
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})

        sc.classify_all(
            _records("tideblock.io"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0, config=SHADOW_OFF,
            excerpt_cache={"someoneelse.dev": _cached()},
        )

        assert calls == ["tideblock.io"]

    def test_failed_refetch_never_overwrites_a_usable_cached_excerpt(
        self, monkeypatch, caplog,
    ):
        # THE INVARIANT. The cached excerpt is stale, so a re-fetch is
        # attempted; archive.org fails (its normal state — 184 of 289 fetches
        # failed on the 2026-09-19 sweep). The good evidence must survive and
        # the domain must NOT be downgraded to unscreened.
        cached = _cached(age_days=400)
        calls = _spy_fetch(monkeypatch, {"tideblock.io": None})
        records = _records("tideblock.io")
        config = {"snapshot_classifier": {"shadow": False,
                                          "excerpt_max_age_days": 90}}

        with caplog.at_level("INFO"):
            counts = sc.classify_all(
                records, client=_FakeClient([_reply({"tideblock.io": "toxic"})]),
                pause_seconds=0.0, config=config,
                excerpt_cache={"tideblock.io": cached},
            )

        assert calls == ["tideblock.io"]                    # the retry happened
        assert records[0]["wayback_excerpt"] == cached      # evidence preserved
        assert records[0]["snapshot_category"] == "toxic"   # still screened
        assert counts["unknown"] == 0
        assert any("1 preserved after a failed re-fetch" in m
                   for m in caplog.messages)

    def test_failed_refetch_of_raw_markup_path_also_preserves(self, monkeypatch):
        # Same invariant on the parked-marker fetch path (markers configured →
        # the composed raw-HTML fetch), where the whole snapshot GET fails.
        cached = _cached(age_days=400)
        _stub_bundle(monkeypatch, {})       # availability returns None
        records = _records("tideblock.io")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"], excerpt_max_age_days=90),
            excerpt_cache={"tideblock.io": cached},
        )

        assert records[0]["wayback_excerpt"] == cached
        assert records[0]["snapshot_category"] == "legitimate"

    def test_stale_excerpt_is_refetched_and_the_new_one_used(
        self, monkeypatch, good_excerpt,
    ):
        cached = _cached(age_days=200)
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})
        records = _records("tideblock.io")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config={"snapshot_classifier": {"shadow": False,
                                            "excerpt_max_age_days": 90}},
            excerpt_cache={"tideblock.io": cached},
        )

        assert calls == ["tideblock.io"]
        assert records[0]["wayback_excerpt"] == good_excerpt

    def test_unparseable_cached_timestamp_is_reused_not_refetched(
        self, monkeypatch,
    ):
        cached = {**_cached(), "snapshot_timestamp": "whenever"}
        calls = _spy_fetch(monkeypatch, {})
        records = _records("tideblock.io")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config={"snapshot_classifier": {"shadow": False,
                                            "excerpt_max_age_days": 90}},
            excerpt_cache={"tideblock.io": cached},
        )

        assert calls == []
        assert records[0]["wayback_excerpt"] == cached

    def test_max_age_zero_reuses_an_ancient_excerpt(self, monkeypatch):
        cached = _cached(age_days=4000)
        calls = _spy_fetch(monkeypatch, {})

        sc.classify_all(
            _records("tideblock.io"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0,
            config={"snapshot_classifier": {"shadow": False,
                                            "excerpt_max_age_days": 0}},
            excerpt_cache={"tideblock.io": cached},
        )

        assert calls == []

    def test_reuse_disabled_restores_todays_behaviour_exactly(
        self, monkeypatch, good_excerpt,
    ):
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})
        records = _records("tideblock.io")
        config = {"snapshot_classifier": {"shadow": False,
                                          "reuse_cached_excerpts": False}}

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=config, excerpt_cache={"tideblock.io": _cached()},
        )

        assert calls == ["tideblock.io"]                   # cache ignored
        assert records[0]["wayback_excerpt"] == good_excerpt

    def test_reuse_disabled_does_not_preserve_cached_on_failure(
        self, monkeypatch,
    ):
        # With reuse off the pre-2026-09-21 outcome is reproduced exactly: a
        # failed fetch means unknown, cache or no cache.
        _spy_fetch(monkeypatch, {"tideblock.io": None})
        records = _records("tideblock.io")
        config = {"snapshot_classifier": {"shadow": False,
                                          "reuse_cached_excerpts": False}}

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=config, excerpt_cache={"tideblock.io": _cached()},
        )

        assert records[0]["wayback_excerpt"] is None
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_no_cache_argument_is_unchanged_behaviour(
        self, monkeypatch, good_excerpt,
    ):
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})
        sc.classify_all(
            _records("tideblock.io"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0, config=SHADOW_OFF,
        )
        assert calls == ["tideblock.io"]

    def test_reused_excerpts_do_not_sleep(self, monkeypatch, good_excerpt):
        # The pacing contract: pause_seconds is archive.org courtesy, so only
        # REAL fetches pay it. This is most of the speed win.
        sleeps: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: sleeps.append(s))
        names = ["tideblock.io", "marketglow.com", "coppernest.org", "lot04test.dev"]
        _spy_fetch(monkeypatch, {n: good_excerpt for n in names})
        cache = {"tideblock.io": _cached(), "marketglow.com": _cached()}

        sc.classify_all(
            _records(*names), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.5, config=SHADOW_OFF, excerpt_cache=cache,
        )

        # 2 reused (no sleep) + 2 fetched → exactly one inter-fetch pause.
        assert sleeps == [0.5]

    def test_all_reused_means_no_sleep_at_all(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(sc.time, "sleep", lambda s: sleeps.append(s))
        names = ["tideblock.io", "marketglow.com"]
        _spy_fetch(monkeypatch, {})

        sc.classify_all(
            _records(*names), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.5, config=SHADOW_OFF,
            excerpt_cache={n: _cached() for n in names},
        )

        assert sleeps == []

    def test_records_without_snapshot_date_ignore_the_cache(self, monkeypatch):
        # No wayback_last_snapshot means no snapshot to reason about at all;
        # a stray cache entry must not resurrect one.
        _spy_fetch(monkeypatch, {})
        record = {"name": "tideblock.io"}

        sc.classify_all(
            [record], client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=SHADOW_OFF, excerpt_cache={"tideblock.io": _cached()},
        )

        assert record["wayback_excerpt"] is None
        assert record["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_empty_cache_dict_behaves_like_no_cache(self, monkeypatch, good_excerpt):
        calls = _spy_fetch(monkeypatch, {"tideblock.io": good_excerpt})
        sc.classify_all(
            _records("tideblock.io"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0, config=SHADOW_OFF, excerpt_cache={},
        )
        assert calls == ["tideblock.io"]

    def test_reuse_is_shadow_aware(self, monkeypatch):
        cached = _cached()
        _spy_fetch(monkeypatch, {})
        records = _records("tideblock.io")

        sc.classify_all(
            records, client=_FakeClient([_reply({"tideblock.io": "toxic"})]),
            pause_seconds=0.0, config=SHADOW_ON,
            excerpt_cache={"tideblock.io": cached},
        )

        assert records[0][sc.SHADOW_FIELD] == "toxic"
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY


# ---------------------------------------------------------------------------
# Parked-page detection from raw markup (no model call)
# ---------------------------------------------------------------------------


class TestParkedMarkerDetection:
    def test_marker_sets_parked_without_a_model_call(self, monkeypatch, caplog):
        _stub_bundle(monkeypatch, {"marketglow.com": PARKED_HTML})
        records = _records("marketglow.com")
        client = _FakeClient(_all("legitimate"))

        with caplog.at_level("INFO"):
            counts = sc.classify_all(
                records, client=client, pause_seconds=0.0,
                config=_markers_config(["data-adblockkey", "sedoparking.com"]),
            )

        assert records[0]["snapshot_category"] == "parked"
        assert client.batches == []          # the model was never asked
        assert counts == {"legitimate": 0, "parked": 1, "toxic": 0,
                          "empty": 0, "unknown": 0}
        assert any("markup marker 'data-adblockkey'" in m for m in caplog.messages)

    def test_parked_page_yields_no_excerpt_but_is_still_categorised(
        self, monkeypatch,
    ):
        # The exact defect: no title / meta / headings → fetch_excerpt's
        # contract says None → the old code could only say "unknown".
        _stub_bundle(monkeypatch, {"marketglow.com": PARKED_HTML})
        records = _records("marketglow.com")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"]),
        )

        assert records[0]["wayback_excerpt"] is None
        assert records[0]["snapshot_category"] == "parked"

    def test_parked_detection_is_shadow_aware(self, monkeypatch):
        _stub_bundle(monkeypatch, {"marketglow.com": PARKED_HTML})
        records = _records("marketglow.com")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"], shadow=True),
        )

        assert records[0][sc.SHADOW_FIELD] == "parked"
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_marker_match_is_case_insensitive(self, monkeypatch):
        html = "<html><body DATA-AdBlockKey=\"ABC\"></body></html>"
        _stub_bundle(monkeypatch, {"marketglow.com": html})
        records = _records("marketglow.com")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"]),
        )

        assert records[0]["snapshot_category"] == "parked"

    @pytest.mark.parametrize("marker", _production_markers())
    def test_every_production_marker_matches_case_insensitively(self, marker):
        html = f"<html><body>PREFIX {marker.upper()} SUFFIX</body></html>"
        assert sc.matched_parked_marker(html, [marker.lower()]) == marker.lower()
        assert sc.matched_parked_marker(html.lower(), [marker.lower()]) is not None

    def test_matched_parked_marker_returns_first_hit(self):
        html = "<html><body>buy this domain via sedoparking.com</body></html>"
        assert sc.matched_parked_marker(
            html, ["sedoparking.com", "buy this domain"]
        ) == "sedoparking.com"

    def test_matched_parked_marker_handles_empty_inputs(self):
        assert sc.matched_parked_marker(None, ["data-adblockkey"]) is None
        assert sc.matched_parked_marker("", ["data-adblockkey"]) is None
        assert sc.matched_parked_marker(PARKED_HTML, []) is None

    def test_real_site_without_a_marker_goes_to_the_model(self, monkeypatch):
        _stub_bundle(monkeypatch, {"coppernest.org": REAL_SITE_HTML})
        records = _records("coppernest.org")
        client = _FakeClient([_reply({"coppernest.org": "legitimate"})])

        sc.classify_all(
            records, client=client, pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"]),
        )

        # The composed raw-markup path must produce fetch_excerpt's shape.
        excerpt = records[0]["wayback_excerpt"]
        assert excerpt["title"] == "Coppernest Woodworking Journal"
        assert excerpt["meta_description"] == "Hand-tool joinery notes."
        assert excerpt["h1"] == ["Hand-tool joinery"]
        assert excerpt["h2"] == ["Recent projects"]
        assert excerpt["snapshot_timestamp"] == "20260901120000"
        assert excerpt["snapshot_url"].endswith("http://coppernest.org/")
        assert client.batches == [["coppernest.org"]]
        assert records[0]["snapshot_category"] == "legitimate"

    def test_no_markers_configured_keeps_the_legacy_fetch_path(
        self, monkeypatch, good_excerpt,
    ):
        calls = _spy_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        bundle_calls = _stub_bundle(monkeypatch, {"coppernest.org": REAL_SITE_HTML})

        sc.classify_all(
            _records("coppernest.org"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert calls == ["coppernest.org"]   # fetch_excerpt, as before
        assert bundle_calls == []            # no raw-markup fetch

    def test_markers_configured_uses_the_raw_markup_path(
        self, monkeypatch, good_excerpt,
    ):
        calls = _spy_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        bundle_calls = _stub_bundle(monkeypatch, {"coppernest.org": REAL_SITE_HTML})

        sc.classify_all(
            _records("coppernest.org"), client=_FakeClient(_all("legitimate")),
            pause_seconds=0.0, config=_markers_config(["data-adblockkey"]),
        )

        assert bundle_calls == ["coppernest.org"]
        assert calls == []                   # exactly ONE fetch, not two

    def test_malformed_markers_config_disables_detection(
        self, monkeypatch, good_excerpt, caplog,
    ):
        calls = _spy_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        config = {"snapshot_classifier": {"shadow": False,
                                          "parked_markers": "data-adblockkey"}}

        with caplog.at_level("WARNING"):
            sc.classify_all(
                _records("coppernest.org"), client=_FakeClient(_all("legitimate")),
                pause_seconds=0.0, config=config,
            )

        assert calls == ["coppernest.org"]
        assert any("parked_markers is not a list" in m for m in caplog.messages)

    def test_snapshot_fetch_failure_on_markup_path_yields_unknown(
        self, monkeypatch,
    ):
        from scripts import wayback_excerpt as we
        _stub_bundle(monkeypatch, {"marketglow.com": PARKED_HTML})
        monkeypatch.setattr(we, "_fetch_snapshot_html", lambda url: None)
        records = _records("marketglow.com")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"]),
        )

        assert records[0]["wayback_excerpt"] is None
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_raising_helper_on_markup_path_fails_soft(self, monkeypatch, caplog):
        from scripts import wayback_excerpt as we
        _stub_bundle(monkeypatch, {"marketglow.com": PARKED_HTML})

        def _boom(domain, target_date):
            raise RuntimeError("availability exploded")

        monkeypatch.setattr(we, "_fetch_availability", _boom)
        records = _records("marketglow.com")

        with caplog.at_level("WARNING"):
            sc.classify_all(
                records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
                config=_markers_config(["data-adblockkey"]),
            )

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert any("snapshot fetch raised" in m for m in caplog.messages)

    def test_missing_helper_falls_back_to_fetch_excerpt(
        self, monkeypatch, good_excerpt, caplog,
    ):
        # wayback_excerpt is not our file; if its internals are renamed we
        # lose parked detection, not the pipeline.
        from scripts import wayback_excerpt as we
        calls = _spy_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        monkeypatch.delattr(we, "_parse_content_signals")
        records = _records("coppernest.org")

        with caplog.at_level("WARNING"):
            sc.classify_all(
                records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
                config=_markers_config(["data-adblockkey"]),
            )

        assert calls == ["coppernest.org"]
        assert records[0]["wayback_excerpt"] == good_excerpt
        assert records[0]["snapshot_category"] == "legitimate"
        assert any("no longer exposes the helpers" in m for m in caplog.messages)

    def test_reused_excerpt_skips_markup_fetch_entirely(self, monkeypatch):
        bundle_calls = _stub_bundle(monkeypatch, {"tideblock.io": PARKED_HTML})
        records = _records("tideblock.io")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(["data-adblockkey"]),
            excerpt_cache={"tideblock.io": _cached()},
        )

        assert bundle_calls == []
        assert records[0]["snapshot_category"] == "legitimate"


# ---------------------------------------------------------------------------
# spam_signatures wiring — the second net
# ---------------------------------------------------------------------------


class TestSignatureOverride:
    def test_model_legitimate_but_signature_fires(self, monkeypatch, caplog):
        cloaked = {
            "snapshot_timestamp": _ts(1),
            "snapshot_url": "http://web.archive.org/web/x/http://marketglow.com/",
            "title": "Orange County machinery — 9001cc金沙以诚为本",
            "meta_description": None,
            "h1": ["Inner Mongolia machinery supply"],
            "h2": [],
        }
        _spy_fetch(monkeypatch, {"marketglow.com": cloaked})
        records = _records("marketglow.com")
        client = _FakeClient([_reply({"marketglow.com": "legitimate"})])

        with caplog.at_level("WARNING"):
            counts = sc.classify_all(
                records, client=client, pause_seconds=0.0, config=SIGNATURE_CONFIG,
            )

        assert records[0]["snapshot_category"] == "toxic"
        assert counts["toxic"] == 1 and counts["legitimate"] == 0
        assert any(
            "SIGNATURE override" in m and "marketglow.com" in m
            and "'legitimate'" in m and "金沙" in m
            for m in caplog.messages
        )
        assert any(
            "signatures fired on 1 domain(s) the model did not call toxic" in m
            for m in caplog.messages
        )

    def test_signature_fires_when_the_model_is_down(self, monkeypatch):
        # The 2026-09-21 story: a token outage published 247 domains with no
        # screening. With signatures, a cached excerpt still screens for free.
        _spy_fetch(monkeypatch, {})
        cached = _cached(title="Bandar casino terpercaya")
        records = _records("tideblock.io")

        counts = sc.classify_all(
            records, client=_FakeClient([LLMBackendError("token expired")]),
            pause_seconds=0.0, config=SIGNATURE_CONFIG,
            excerpt_cache={"tideblock.io": cached},
        )

        assert records[0]["snapshot_category"] == "toxic"
        assert counts["toxic"] == 1

    def test_signature_can_override_a_parked_verdict(self, monkeypatch):
        html = (
            "<html><head><title>Casino bonus</title></head>"
            "<body data-adblockkey=\"K\"><h1>Situs</h1></body></html>"
        )
        _stub_bundle(monkeypatch, {"marketglow.com": html})
        records = _records("marketglow.com")

        sc.classify_all(
            records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
            config=_markers_config(
                ["data-adblockkey"],
                signature_action="toxic",
                signature_terms_latin=["casino"],
            ),
        )

        assert records[0]["snapshot_category"] == "toxic"

    def test_model_toxic_is_not_re_logged_as_an_override(self, monkeypatch, caplog):
        _spy_fetch(monkeypatch, {"marketglow.com": _cached(title="Casino bonus")})
        records = _records("marketglow.com")
        client = _FakeClient([_reply({"marketglow.com": "toxic"})])

        with caplog.at_level("WARNING"):
            sc.classify_all(
                records, client=client, pause_seconds=0.0, config=SIGNATURE_CONFIG,
            )

        assert records[0]["snapshot_category"] == "toxic"
        assert not any("SIGNATURE override" in m for m in caplog.messages)

    def test_clean_excerpt_is_not_overridden(self, monkeypatch, good_excerpt):
        _spy_fetch(monkeypatch, {"coppernest.org": good_excerpt})
        records = _records("coppernest.org")

        sc.classify_all(
            records, client=_FakeClient([_reply({"coppernest.org": "legitimate"})]),
            pause_seconds=0.0, config=SIGNATURE_CONFIG,
        )

        assert records[0]["snapshot_category"] == "legitimate"

    def test_invalid_signature_action_leaves_the_verdict_alone(
        self, monkeypatch, caplog,
    ):
        _spy_fetch(monkeypatch, {"marketglow.com": _cached(title="Casino bonus")})
        records = _records("marketglow.com")
        config = {"snapshot_classifier": {
            "shadow": False, "signature_action": "quarantine",
            "signature_terms_latin": ["casino"],
        }}

        with caplog.at_level("WARNING"):
            sc.classify_all(
                records,
                client=_FakeClient([_reply({"marketglow.com": "legitimate"})]),
                pause_seconds=0.0, config=config,
            )

        assert records[0]["snapshot_category"] == "legitimate"
        assert any("is not a category" in m for m in caplog.messages)

    def test_signature_override_is_shadow_aware(self, monkeypatch):
        _spy_fetch(monkeypatch, {"marketglow.com": _cached(title="Casino bonus")})
        records = _records("marketglow.com")
        config = {"snapshot_classifier": {
            "shadow": True, "signature_action": "toxic",
            "signature_terms_latin": ["casino"],
        }}

        sc.classify_all(
            records, client=_FakeClient([_reply({"marketglow.com": "legitimate"})]),
            pause_seconds=0.0, config=config,
        )

        assert records[0][sc.SHADOW_FIELD] == "toxic"
        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY

    def test_no_signature_terms_configured_changes_nothing(self, monkeypatch):
        _spy_fetch(monkeypatch, {"marketglow.com": _cached(title="Casino bonus")})
        records = _records("marketglow.com")

        sc.classify_all(
            records, client=_FakeClient([_reply({"marketglow.com": "legitimate"})]),
            pause_seconds=0.0, config=SHADOW_OFF,
        )

        assert records[0]["snapshot_category"] == "legitimate"

    def test_records_without_an_excerpt_are_not_scanned(self, monkeypatch, caplog):
        _spy_fetch(monkeypatch, {"marketglow.com": None})
        records = _records("marketglow.com")

        with caplog.at_level("WARNING"):
            sc.classify_all(
                records, client=_FakeClient(_all("legitimate")), pause_seconds=0.0,
                config=SIGNATURE_CONFIG,
            )

        assert records[0]["snapshot_category"] == sc.UNKNOWN_CATEGORY
        assert not any("SIGNATURE" in m for m in caplog.messages)

    def test_canonical_summary_line_is_still_last(self, monkeypatch, caplog):
        # send_report.py parses the canonical line; the new override and
        # evidence lines must not displace it.
        _spy_fetch(monkeypatch, {"marketglow.com": _cached(title="Casino bonus")})

        with caplog.at_level("INFO"):
            sc.classify_all(
                _records("marketglow.com"),
                client=_FakeClient([_reply({"marketglow.com": "legitimate"})]),
                pause_seconds=0.0, config=SIGNATURE_CONFIG,
            )

        assert caplog.messages[-1].startswith("snapshot_classifier: results — ")
