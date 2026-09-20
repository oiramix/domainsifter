"""Unit tests for scripts/toxic_denylist.py.

R2 is mocked entirely with MagicMock (same approach as the
record_overflow tests in tests/test_phase2_ranker.py) — nothing here
touches a live endpoint or needs credentials.

All domains are invented (hard rule 1).
"""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from scripts import toxic_denylist

TODAY = date(2026, 9, 20)


def _no_such_key() -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )


def _body(*records: dict) -> bytes:
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8")


def _r2_with(raw: bytes) -> tuple[MagicMock, dict]:
    """MagicMock R2 client whose get_object returns `raw`, plus a dict that
    captures whatever put_object is called with."""
    r2 = MagicMock()
    r2.get_object.return_value = {"Body": BytesIO(raw)}
    captured: dict = {}

    def put_object(Bucket, Key, Body, ContentType):
        captured["bucket"] = Bucket
        captured["key"] = Key
        captured["body"] = Body
        captured["content_type"] = ContentType

    r2.put_object.side_effect = put_object
    return r2, captured


def _records_from(captured: dict) -> list[dict]:
    return [
        json.loads(line)
        for line in captured["body"].decode("utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# load_denylist
# ---------------------------------------------------------------------------


def test_load_returns_names():
    r2, _ = _r2_with(_body(
        {"name": "tideblock.io", "classified_date": "2026-09-19",
         "classifier_version": "v2"},
        {"name": "coppernest.org", "classified_date": "2026-09-18",
         "classifier_version": "v2"},
    ))
    names = toxic_denylist.load_denylist(r2_client=r2, r2_bucket="test-bucket")
    assert names == {"tideblock.io", "coppernest.org"}
    r2.get_object.assert_called_once_with(
        Bucket="test-bucket", Key="state/toxic_denylist.jsonl",
    )


def test_load_lowercases_and_strips_names():
    """Lookup in the filter is lowercase; the stored form must not matter."""
    r2, _ = _r2_with(_body(
        {"name": "  TideBlock.IO  ", "classified_date": "2026-09-19",
         "classifier_version": "v2"},
    ))
    assert toxic_denylist.load_denylist(
        r2_client=r2, r2_bucket="test-bucket",
    ) == {"tideblock.io"}


def test_load_on_cold_start_returns_empty_set():
    """No object yet is a cold start, not an error."""
    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    assert toxic_denylist.load_denylist(
        r2_client=r2, r2_bucket="test-bucket",
    ) == set()


def test_load_on_r2_error_returns_empty_set_and_does_not_raise(caplog):
    """Hard rule 17 — a dead R2 degrades the gate, it never blocks a run."""
    r2 = MagicMock()
    r2.get_object.side_effect = RuntimeError("R2 outage")
    names = toxic_denylist.load_denylist(r2_client=r2, r2_bucket="test-bucket")
    assert names == set()
    assert any(
        rec.levelname == "ERROR" and "LOAD FAILED" in rec.message
        for rec in caplog.records
    ), "a lost denylist must be logged loudly, not silently"


def test_load_skips_malformed_and_partial_jsonl_lines():
    """One truncated line must not cost us every other remembered verdict."""
    raw = b"\n".join([
        json.dumps({"name": "tideblock.io", "classified_date": "2026-09-19",
                    "classifier_version": "v2"}).encode(),
        b'{"name": "truncated.org", "classified_da',   # partial write
        b"",                                            # blank line
        b'"just a string, not an object"',              # valid JSON, wrong type
        json.dumps({"classified_date": "2026-09-19"}).encode(),  # no name
        json.dumps({"name": "", "classified_date": "2026-09-19"}).encode(),
        json.dumps({"name": "coppernest.org", "classified_date": "2026-09-18",
                    "classifier_version": "v2"}).encode(),
    ]) + b"\n"
    r2, _ = _r2_with(raw)
    assert toxic_denylist.load_denylist(
        r2_client=r2, r2_bucket="test-bucket",
    ) == {"tideblock.io", "coppernest.org"}


# ---------------------------------------------------------------------------
# record_toxic
# ---------------------------------------------------------------------------


def test_record_writes_expected_record():
    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    captured: dict = {}
    r2.put_object.side_effect = lambda Bucket, Key, Body, ContentType: captured.update(
        bucket=Bucket, key=Key, body=Body,
    )

    n = toxic_denylist.record_toxic(
        ["tideblock.io"],
        today=TODAY,
        classifier_version="v2",
        r2_client=r2,
        r2_bucket="test-bucket",
    )
    assert n == 1
    assert captured["key"] == "state/toxic_denylist.jsonl"
    assert _records_from(captured) == [{
        "name": "tideblock.io",
        "classified_date": "2026-09-20",
        "classifier_version": "v2",
    }]


def test_record_appends_without_rewriting_existing_entries():
    """Append-only: prior records survive verbatim, including their original
    classified_date. Nothing is ever aged out."""
    prior = {"name": "coppernest.org", "classified_date": "2026-01-05",
             "classifier_version": "v1"}
    r2, captured = _r2_with(_body(prior))

    n = toxic_denylist.record_toxic(
        ["tideblock.io"],
        today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 1
    records = _records_from(captured)
    assert records[0] == prior          # untouched, not re-dated
    assert records[1]["name"] == "tideblock.io"
    assert len(records) == 2


def test_record_is_idempotent():
    """Re-recording a remembered domain must not duplicate it."""
    r2, captured = _r2_with(_body(
        {"name": "tideblock.io", "classified_date": "2026-09-19",
         "classifier_version": "v2"},
    ))
    n = toxic_denylist.record_toxic(
        ["tideblock.io"],
        today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 0
    r2.put_object.assert_not_called()   # nothing new → no write at all
    assert captured == {}


def test_record_writes_only_the_new_names_in_a_mixed_batch():
    r2, captured = _r2_with(_body(
        {"name": "tideblock.io", "classified_date": "2026-09-19",
         "classifier_version": "v2"},
    ))
    n = toxic_denylist.record_toxic(
        ["tideblock.io", "marketglow.com", "TideBlock.io"],
        today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 1
    names = [r["name"] for r in _records_from(captured)]
    assert names == ["tideblock.io", "marketglow.com"]


def test_record_dedupes_within_the_incoming_batch():
    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    captured: dict = {}
    r2.put_object.side_effect = lambda Bucket, Key, Body, ContentType: captured.update(body=Body)

    n = toxic_denylist.record_toxic(
        ["marketglow.com", "marketglow.com", "  MARKETGLOW.com "],
        today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 1
    assert len(_records_from(captured)) == 1


def test_record_noop_on_empty_input():
    r2 = MagicMock()
    assert toxic_denylist.record_toxic(
        [], today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    ) == 0
    r2.get_object.assert_not_called()
    r2.put_object.assert_not_called()


def test_record_swallows_r2_read_errors(caplog):
    r2 = MagicMock()
    r2.get_object.side_effect = RuntimeError("R2 outage")
    n = toxic_denylist.record_toxic(
        ["tideblock.io"], today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    )
    assert n == 0
    assert any("APPEND FAILED" in rec.message for rec in caplog.records)


def test_record_swallows_r2_write_errors():
    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    r2.put_object.side_effect = RuntimeError("R2 write rejected")
    assert toxic_denylist.record_toxic(
        ["tideblock.io"], today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    ) == 0


def test_round_trip_load_sees_what_record_wrote():
    """The two halves agree on the record shape."""
    r2 = MagicMock()
    r2.get_object.side_effect = _no_such_key()
    captured: dict = {}
    r2.put_object.side_effect = lambda Bucket, Key, Body, ContentType: captured.update(body=Body)

    toxic_denylist.record_toxic(
        ["tideblock.io", "coppernest.org"],
        today=TODAY, classifier_version="v2",
        r2_client=r2, r2_bucket="test-bucket",
    )
    r2b, _ = _r2_with(captured["body"])
    assert toxic_denylist.load_denylist(
        r2_client=r2b, r2_bucket="test-bucket",
    ) == {"tideblock.io", "coppernest.org"}


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_defaults_true_when_section_absent():
    assert toxic_denylist.is_enabled({}) is True
    assert toxic_denylist.is_enabled({"toxic_denylist": {}}) is True


def test_is_enabled_respects_explicit_false():
    assert toxic_denylist.is_enabled({"toxic_denylist": {"enabled": False}}) is False
