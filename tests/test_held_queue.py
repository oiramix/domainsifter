"""Unit tests for scripts/held_queue.py.

The pipeline-level behaviour (held domains not published, released after a
successful re-check, expiry, RDAP re-registration, never in the newsletter)
is covered end-to-end by the test_main_held_queue_* tests in
tests/test_pipeline.py. These tests pin the pure decision logic and the R2
plumbing. R2 is always a MagicMock; nothing touches the network.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from io import BytesIO
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from scripts import held_queue

TODAY = date(2026, 10, 3)
CFG = {
    "held_queue": {
        "enabled": True,
        "max_held_days": 3,
        "required_fields": ["wayback_snapshots", "open_page_rank"],
    },
}
SCREENED = {
    "snapshot_category": "legitimate",
    "wayback_snapshots": 40,
    "open_page_rank": 2.0,
}


def _cand(name: str, **fields) -> dict:
    return {"name": name, "tld": name.rsplit(".", 1)[-1], **fields}


def _s3_with(body: bytes) -> MagicMock:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": BytesIO(body)}
    return s3


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_enabled_by_default_when_section_missing():
    assert held_queue.is_enabled({}) is True
    assert held_queue.is_enabled({"held_queue": {"enabled": False}}) is False


def test_max_held_days_default_and_bad_value():
    assert held_queue.max_held_days({}) == 3
    assert held_queue.max_held_days({"held_queue": {"max_held_days": "x"}}) == 3
    assert held_queue.max_held_days({"held_queue": {"max_held_days": -2}}) == 0


def test_required_fields_default():
    assert held_queue.required_fields({}) == ("wayback_snapshots", "open_page_rank")


# ---------------------------------------------------------------------------
# incomplete_reasons
# ---------------------------------------------------------------------------


def test_fully_screened_candidate_has_no_reasons():
    assert held_queue.incomplete_reasons(_cand("marketglow.com", **SCREENED), CFG) == []


def test_parked_and_empty_are_real_verdicts():
    for category in ("parked", "empty"):
        cand = _cand("marketglow.com", **{**SCREENED, "snapshot_category": category})
        assert held_queue.incomplete_reasons(cand, CFG) == []


def test_unknown_or_missing_category_is_unscreened():
    for category in ("unknown", None):
        cand = _cand("marketglow.com", **{**SCREENED, "snapshot_category": category})
        assert held_queue.incomplete_reasons(cand, CFG) == ["content_unscreened"]


def test_wayback_unknown_and_missing_fields_are_listed():
    cand = _cand(
        "marketglow.com",
        snapshot_category="legitimate",
        wayback_unknown=True,
        open_page_rank=None,
    )
    assert held_queue.incomplete_reasons(cand, CFG) == [
        "wayback_unknown",
        "missing:wayback_snapshots",
        "missing:open_page_rank",
    ]


def test_cert_history_not_required_by_default():
    cand = _cand("marketglow.com", **SCREENED)
    assert "cert_history" not in cand
    assert held_queue.incomplete_reasons(cand, CFG) == []


def test_zero_open_page_rank_counts_as_present():
    cand = _cand("marketglow.com", **{**SCREENED, "open_page_rank": 0})
    assert held_queue.incomplete_reasons(cand, CFG) == []


# ---------------------------------------------------------------------------
# to_candidates
# ---------------------------------------------------------------------------


def test_to_candidates_carries_identity_and_ranker_context_only():
    records = [
        {"name": "TideBlock.com", "tld": "com", "dropped_date": "2026-10-01",
         "phase2_score": 77, "phase2_reason": "short, brandable",
         "first_held_date": "2026-10-02", "checks": 1, "reasons": ["x"]},
        {"name": "tideblock.com"},  # duplicate after lowercasing
        {"name": ""},
    ]
    cands = held_queue.to_candidates(records)
    assert cands == [{
        "name": "tideblock.com", "tld": "com", "dropped_date": "2026-10-01",
        "phase2_score": 77, "phase2_reason": "short, brandable",
        "held_since": "2026-10-02",
    }]


def test_to_candidates_derives_tld_when_missing():
    assert held_queue.to_candidates([{"name": "coppernest.org"}])[0]["tld"] == "org"


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------


def _settle(held_records, held_candidates, new_available, enriched, survivors, cfg=CFG):
    return held_queue.settle(
        held_records=held_records,
        held_candidates=held_candidates,
        new_available=new_available,
        enriched=enriched,
        survivors=survivors,
        config=cfg,
        today=TODAY,
    )


def test_settle_new_screened_publishes_and_unscreened_is_held():
    good = _cand("marketglow.com", is_available=True, **SCREENED)
    bad = _cand("tideblock.com", is_available=True,
                **{**SCREENED, "snapshot_category": "unknown"}, phase2_score=81)
    publish, queue, outcome = _settle([], [], [good, bad], [good, bad], [good, bad])

    assert publish == [good]
    assert queue == [{
        "name": "tideblock.com", "tld": "com", "phase2_score": 81,
        "first_held_date": "2026-10-03", "last_checked_date": "2026-10-03",
        "checks": 0, "reasons": ["content_unscreened"],
    }]
    assert outcome["new"] == ["tideblock.com"]
    assert outcome["held"] == 1


def test_settle_new_candidate_rejected_by_filter_is_not_held():
    toxic = _cand("tideblock.com", is_available=True, snapshot_category="toxic")
    publish, queue, outcome = _settle([], [], [toxic], [toxic], [])
    assert publish == [] and queue == [] and outcome["new"] == []


def test_settle_new_candidate_skipped_by_enrichment_budget_is_held():
    skipped = _cand("tideblock.com", is_available=True)
    publish, queue, _ = _settle([], [], [skipped], [], [])
    assert publish == []
    assert queue[0]["reasons"] == ["not_enriched"]


def test_settle_releases_held_domain_once_screened():
    rec = {"name": "tideblock.com", "first_held_date": "2026-10-02", "checks": 0}
    cand = held_queue.to_candidates([rec])[0]
    cand.update(is_available=True, **SCREENED)
    publish, queue, outcome = _settle([rec], [cand], [], [cand], [cand])
    assert publish == [cand]
    assert queue == []
    assert outcome["released"] == ["tideblock.com"]


def test_settle_keeps_held_domain_still_incomplete_and_counts_checks():
    rec = {"name": "tideblock.com", "tld": "com", "dropped_date": "2026-10-01",
           "first_held_date": "2026-10-01", "checks": 1}
    cand = held_queue.to_candidates([rec])[0]
    cand.update(is_available=True, snapshot_category="unknown",
                wayback_snapshots=5, open_page_rank=1.0)
    publish, queue, outcome = _settle([rec], [cand], [], [cand], [cand])
    assert publish == []
    assert queue == [{
        "name": "tideblock.com", "tld": "com", "dropped_date": "2026-10-01",
        "first_held_date": "2026-10-01", "last_checked_date": "2026-10-03",
        "checks": 2, "reasons": ["content_unscreened"],
    }]
    assert outcome["kept"] == ["tideblock.com"]


def test_settle_expires_after_max_held_days():
    rec = {"name": "tideblock.com", "first_held_date": "2026-09-30", "checks": 2}
    cand = held_queue.to_candidates([rec])[0]
    cand.update(is_available=True, snapshot_category="unknown")
    publish, queue, outcome = _settle([rec], [cand], [], [cand], [cand])
    assert publish == [] and queue == []
    assert outcome["expired"] == ["tideblock.com"]


def test_settle_zero_window_drops_incomplete_same_day():
    cfg = {"held_queue": {"max_held_days": 0}}
    bad = _cand("tideblock.com", is_available=True, snapshot_category="unknown")
    publish, queue, outcome = _settle([], [], [bad], [bad], [bad], cfg=cfg)
    assert publish == [] and queue == []
    assert outcome["expired"] == ["tideblock.com"]


def test_settle_reregistered_held_domain_is_dropped():
    rec = {"name": "tideblock.com", "first_held_date": "2026-10-02"}
    cand = held_queue.to_candidates([rec])[0]
    cand["is_available"] = False
    publish, queue, outcome = _settle([rec], [cand], [], [], [])
    assert publish == [] and queue == []
    assert outcome["reregistered"] == ["tideblock.com"]


def test_settle_held_domain_rejected_on_recheck_is_dropped():
    rec = {"name": "tideblock.com", "first_held_date": "2026-10-02"}
    cand = held_queue.to_candidates([rec])[0]
    cand.update(is_available=True, snapshot_category="toxic")
    publish, queue, outcome = _settle([rec], [cand], [], [cand], [])
    assert publish == [] and queue == []
    assert outcome["rejected"] == ["tideblock.com"]


def test_settle_rdap_unknown_held_domain_stays_held():
    rec = {"name": "tideblock.com", "first_held_date": "2026-10-02"}
    cand = held_queue.to_candidates([rec])[0]
    cand["is_available"] = None
    publish, queue, _ = _settle([rec], [cand], [], [], [])
    assert publish == []
    assert queue[0]["reasons"] == ["rdap_unknown"]


def test_settle_corrupt_first_held_date_restarts_window_once():
    rec = {"name": "tideblock.com", "first_held_date": "not-a-date"}
    cand = held_queue.to_candidates([rec])[0]
    cand.update(is_available=True, snapshot_category="unknown")
    _, queue, _ = _settle([rec], [cand], [], [cand], [cand])
    assert queue[0]["first_held_date"] == "2026-10-03"


def test_settle_duplicate_held_records_settle_once():
    rec = {"name": "tideblock.com", "first_held_date": "2026-10-02"}
    cand = held_queue.to_candidates([rec])[0]
    cand.update(is_available=True, snapshot_category="unknown")
    _, queue, _ = _settle([rec, dict(rec)], [cand], [], [cand], [cand])
    assert len(queue) == 1


# ---------------------------------------------------------------------------
# R2 plumbing
# ---------------------------------------------------------------------------


def test_load_cold_start_returns_empty_list():
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "nope"}}, "GetObject",
    )
    assert held_queue.load_held(r2_client=s3, r2_bucket="b") == []


def test_load_skips_corrupt_lines():
    body = b'{"name": "tideblock.com"}\nnot json\n{"no_name": 1}\n\n{"name": "coppernest.org"}\n'
    records = held_queue.load_held(r2_client=_s3_with(body), r2_bucket="b")
    assert [r["name"] for r in records] == ["tideblock.com", "coppernest.org"]


def test_load_failure_returns_none_not_empty(caplog):
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject",
    )
    with caplog.at_level(logging.ERROR, logger="scripts.held_queue"):
        assert held_queue.load_held(r2_client=s3, r2_bucket="b") is None
    assert "LOAD FAILED" in caplog.text


def test_save_writes_jsonl_to_the_private_key():
    s3 = MagicMock()
    records = [{"name": "tideblock.com", "checks": 0}, {"name": "coppernest.org"}]
    assert held_queue.save_held(records, r2_client=s3, r2_bucket="b") is True
    kwargs = s3.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "b"
    assert kwargs["Key"] == "state/held_queue.jsonl"
    lines = kwargs["Body"].decode("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == records


def test_save_empty_queue_writes_empty_object():
    s3 = MagicMock()
    assert held_queue.save_held([], r2_client=s3, r2_bucket="b") is True
    assert s3.put_object.call_args.kwargs["Body"] == b""


def test_save_failure_returns_false_without_raising():
    s3 = MagicMock()
    s3.put_object.side_effect = RuntimeError("r2 down")
    assert held_queue.save_held([{"name": "tideblock.com"}], r2_client=s3, r2_bucket="b") is False


def test_round_trip_through_save_and_load():
    s3 = MagicMock()
    records = [{"name": "tideblock.com", "reasons": ["content_unscreened"]}]
    held_queue.save_held(records, r2_client=s3, r2_bucket="b")
    body = s3.put_object.call_args.kwargs["Body"]
    assert held_queue.load_held(r2_client=_s3_with(body), r2_bucket="b") == records


# ---------------------------------------------------------------------------
# log_outcome — the contract line send_report.py parses
# ---------------------------------------------------------------------------


def test_log_outcome_contract_line(caplog):
    outcome = {
        "held": 4, "new": ["a.com", "b.com"], "kept": ["c.com", "d.com"],
        "released": ["e.com"], "expired": ["f.com", "g.com"],
        "reregistered": [], "rejected": ["h.com"],
        "reasons": {"content_unscreened": 3, "missing:open_page_rank": 1},
    }
    with caplog.at_level(logging.INFO, logger="scripts.held_queue"):
        held_queue.log_outcome(outcome, saved=True)
    assert (
        "Held queue: held=4 new=2 released=1 expired=2 reregistered=0 rejected=1"
        in caplog.text
    )
    assert "Held queue released: e.com" in caplog.text
    assert "Held queue expired: f.com, g.com" in caplog.text
    assert "SAVE" not in caplog.text


def test_log_outcome_reports_save_failure_and_skip(caplog):
    outcome = {"held": 2, "new": ["a.com", "b.com"]}
    with caplog.at_level(logging.INFO, logger="scripts.held_queue"):
        held_queue.log_outcome(outcome, saved=False)
        held_queue.log_outcome(outcome, saved=None)
    assert "Held queue: SAVE FAILED" in caplog.text
    assert "Held queue: SAVE SKIPPED" in caplog.text
