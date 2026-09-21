"""Unit tests for scripts/classify_carryover.py.

Network + git mocked. The orchestration is the focus — filter_targets,
split_toxic, update_counts, build_sidecar_updates, the dry-run vs.
live-mode branching, and the toxic eviction's effect on top-level counts.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import classify_carryover as cc
from scripts import snapshot_classifier as sc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_payload():
    """A minimal but realistic daily-domains.json payload with a mix of
    classification states. Used as the starting state for end-to-end tests."""
    return {
        "generated_at": "2026-05-18T13:11:17Z",
        "domain_count": 4,
        "today_count": 1,
        "carryover_count": 3,
        "domains": [
            {
                "name": "alpha.com", "tld": "com",
                "wayback_last_snapshot": "2025-12-01", "score": 75,
                "days_listed": 0,
                # No snapshot_category — should be picked up in default mode
            },
            {
                "name": "bravo.net", "tld": "net",
                "wayback_last_snapshot": "2025-11-15", "score": 60,
                "days_listed": 2,
                "snapshot_category": "unknown",   # picked up in --only-unknown
            },
            {
                "name": "charlie.org", "tld": "org",
                "wayback_last_snapshot": "2025-10-20", "score": 50,
                "days_listed": 5,
                "snapshot_category": "legitimate",  # skipped in default mode
            },
            {
                "name": "delta.io", "tld": "io",
                "wayback_last_snapshot": None, "score": 30,
                "days_listed": 10,
                "snapshot_category": "unknown",
                # snapshot_category=unknown BUT no wayback_last_snapshot →
                # excluded by --only-unknown (would always re-derive unknown)
            },
        ],
    }


@pytest.fixture
def tmp_paths(tmp_path):
    """Fresh temp paths for daily-domains + sidecar."""
    return {
        "daily": tmp_path / "daily-domains.json",
        "sidecar": tmp_path / "wayback_excerpts.json",
    }


@pytest.fixture
def write_payload(tmp_paths):
    """Helper: write an arbitrary payload to the tmp daily-domains path."""
    def _w(payload):
        tmp_paths["daily"].write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
    return _w


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip pacing sleep across classify_all in every test."""
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)


class _ScriptedClient:
    """Batched fake: replies with one scripted category per domain in a batch.

    Mirrors the real `BatchClassifierClient` contract since the 2026-09-18
    switch — `classify_batch` returns the model's RAW reply text, which the
    classifier then runs through `llm_backend.parse_json_array`. Categories
    are consumed in order across all batches, so a test scripting
    ["legitimate", "toxic", "parked"] labels the first three domains in the
    order the classifier presents them.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def classify_batch(self, user: str, *, timeout_seconds: int | None = None) -> str:
        self.calls.append(user)
        # The user turn ends with the JSON payload of {"domain": ...} objects;
        # pull the domains back out so the reply lines up one-for-one.
        payload = json.loads(user[user.index("[") :])
        rows = []
        for item in payload:
            category = self._responses.pop(0) if self._responses else "legitimate"
            rows.append({"domain": item["domain"], "category": category})
        return json.dumps(rows)


def _stub_fetch(monkeypatch, by_name):
    def _stub(name, target_date):
        return by_name.get(name)
    monkeypatch.setattr("scripts.wayback_excerpt.fetch_excerpt", _stub)


# --- Excerpt-reuse test doubles --------------------------------------------
#
# The reuse tests mock snapshot_classifier.classify_all outright: what is
# under test is how classify_carryover feeds the sidecar in and merges it
# back out, not the classifier's own fetch logic. Nothing here touches the
# network.


class _ClassifyAllSpy:
    """Stand-in for snapshot_classifier.classify_all.

    Records the kwargs of every call, then writes onto each candidate the
    same three fields the real function writes: wayback_excerpt (scripted per
    domain — None models a fetch that failed this run), snapshot_category and
    the version stamp.

    `raise_on_cache` makes the spy reject an `excerpt_cache` kwarg, which is
    how the older-signature fallback path gets exercised.
    """

    def __init__(self, *, excerpts=None, categories=None, raise_on_cache=None):
        self.excerpts: dict[str, dict | None] = dict(excerpts or {})
        self.categories: dict[str, str] = dict(categories or {})
        self.raise_on_cache: TypeError | None = raise_on_cache
        self.calls: list[dict] = []

    def __call__(self, candidates: list[dict], **kwargs) -> dict[str, int]:
        self.calls.append(kwargs)
        if self.raise_on_cache is not None and "excerpt_cache" in kwargs:
            raise self.raise_on_cache
        counts = {c: 0 for c in (*sc.VALID_CATEGORIES, sc.UNKNOWN_CATEGORY)}
        for record in candidates:
            name = record.get("name", "")
            record["wayback_excerpt"] = self.excerpts.get(name)
            category = self.categories.get(name, sc.UNKNOWN_CATEGORY)
            record["snapshot_category"] = category
            record["snapshot_classifier_version"] = sc.CLASSIFIER_VERSION
            counts[category] = counts.get(category, 0) + 1
        return counts

    @property
    def last_kwargs(self) -> dict:
        return self.calls[-1]


def _legacy_classify_all(
    candidates: list[dict], *, client=None, pause_seconds: float = 1.0,
    config: dict | None = None,
) -> dict[str, int]:
    """The pre-2026-09-21 signature, with NO excerpt_cache parameter. Used to
    prove classify_carryover survives landing before the classifier change."""
    _legacy_classify_all.calls.append(len(candidates))
    counts = {c: 0 for c in (*sc.VALID_CATEGORIES, sc.UNKNOWN_CATEGORY)}
    for record in candidates:
        record["wayback_excerpt"] = {"title": "fetched fresh"}
        record["snapshot_category"] = "legitimate"
        record["snapshot_classifier_version"] = sc.CLASSIFIER_VERSION
        counts["legitimate"] += 1
    return counts


_legacy_classify_all.calls = []


# Invented names only (hard rule 1) — none of these are real registrations.
STORED_EXCERPT = {"title": "Coppernest Pottery Studio", "h1": ["Hand-thrown"]}
UNRELATED_EXCERPT = {"title": "Driftlantern Sailing Club"}


@pytest.fixture
def reuse_payload():
    """Two unclassified entries: one we have a stored excerpt for, one we
    do not."""
    return {
        "generated_at": "2026-09-21T06:40:00Z",
        "domain_count": 2, "today_count": 2, "carryover_count": 0,
        "domains": [
            {
                "name": "coppernest.org", "tld": "org", "score": 80,
                "days_listed": 0, "wayback_last_snapshot": "2025-08-14",
            },
            {
                "name": "tideblock.io", "tld": "io", "score": 70,
                "days_listed": 0, "wayback_last_snapshot": "2025-07-02",
            },
        ],
    }


@pytest.fixture
def write_sidecar(tmp_paths):
    def _w(mapping):
        tmp_paths["sidecar"].write_text(
            json.dumps(mapping, indent=2), encoding="utf-8",
        )
    return _w


# Keeps the durable toxic denylist (a real repo-state file) out of every
# live-mode reuse test; eviction behaviour has its own coverage above.
NO_DENYLIST = {"toxic_denylist": {"enabled": False}}


def _reuse_config(enabled: bool | None = None) -> dict:
    config: dict = dict(NO_DENYLIST)
    if enabled is not None:
        config["snapshot_classifier"] = {"reuse_cached_excerpts": enabled}
    return config


# ---------------------------------------------------------------------------
# filter_targets
# ---------------------------------------------------------------------------


class TestFilterTargets:
    def test_default_mode_selects_entries_without_category(self, sample_payload):
        targets = cc.filter_targets(
            sample_payload["domains"], force=False, only_unknown=False, limit=None,
        )
        names = [d["name"] for d in targets]
        assert names == ["alpha.com"]  # only entry without snapshot_category

    def test_only_unknown_selects_unknowns_with_snapshot(self, sample_payload):
        targets = cc.filter_targets(
            sample_payload["domains"], force=False, only_unknown=True, limit=None,
        )
        names = [d["name"] for d in targets]
        # bravo.net: unknown + has snapshot → IN
        # delta.io: unknown but no snapshot → OUT
        # charlie.org: legitimate → OUT
        # alpha.com: no category → OUT
        assert names == ["bravo.net"]

    def test_force_selects_every_entry(self, sample_payload):
        targets = cc.filter_targets(
            sample_payload["domains"], force=True, only_unknown=False, limit=None,
        )
        assert len(targets) == 4

    def test_limit_caps_to_top_n_by_score(self, sample_payload):
        # All four pass when force=True; --limit 2 picks the two highest scores.
        targets = cc.filter_targets(
            sample_payload["domains"], force=True, only_unknown=False, limit=2,
        )
        assert len(targets) == 2
        assert targets[0]["name"] == "alpha.com"   # score=75
        assert targets[1]["name"] == "bravo.net"   # score=60

    def test_limit_zero_returns_empty(self, sample_payload):
        targets = cc.filter_targets(
            sample_payload["domains"], force=True, only_unknown=False, limit=0,
        )
        assert targets == []


# ---------------------------------------------------------------------------
# split_toxic
# ---------------------------------------------------------------------------


class TestSplitToxic:
    def test_only_toxic_evicted(self):
        domains = [
            {"name": "a.com", "snapshot_category": "legitimate"},
            {"name": "b.net", "snapshot_category": "toxic"},
            {"name": "c.org", "snapshot_category": "parked"},
            {"name": "d.io", "snapshot_category": "unknown"},
            {"name": "e.com", "snapshot_category": "empty"},
            {"name": "f.net", "snapshot_category": "toxic"},
        ]
        kept, evicted = cc.split_toxic(domains)

        assert [d["name"] for d in kept] == ["a.com", "c.org", "d.io", "e.com"]
        assert evicted == ["b.net", "f.net"]

    def test_unlabeled_passes_through(self):
        # Entries without snapshot_category at all: kept (not toxic).
        domains = [{"name": "a.com"}]
        kept, evicted = cc.split_toxic(domains)
        assert kept == domains
        assert evicted == []

    def test_empty_input(self):
        assert cc.split_toxic([]) == ([], [])


# ---------------------------------------------------------------------------
# update_counts
# ---------------------------------------------------------------------------


class TestUpdateCounts:
    def test_recompute_after_eviction(self):
        payload = {
            "domain_count": 99, "today_count": 99, "carryover_count": 99,
            "generated_at": "should-be-preserved",
            "domains": [
                {"name": "a", "days_listed": 0},
                {"name": "b", "days_listed": 0},
                {"name": "c", "days_listed": 3},
                {"name": "d", "days_listed": 7},
            ],
        }
        cc.update_counts(payload)
        assert payload["domain_count"] == 4
        assert payload["today_count"] == 2
        assert payload["carryover_count"] == 2
        assert payload["generated_at"] == "should-be-preserved"  # untouched

    def test_zero_domains_all_zero(self):
        payload = {"domains": []}
        cc.update_counts(payload)
        assert payload == {
            "domains": [],
            "domain_count": 0, "today_count": 0, "carryover_count": 0,
        }

    def test_missing_days_listed_counts_as_carryover(self):
        # An entry without days_listed key: treated as days_listed=0 → today
        # (matches output.py's `d.get("days_listed") or 0` convention).
        payload = {"domains": [{"name": "a"}]}
        cc.update_counts(payload)
        assert payload["today_count"] == 1  # missing → 0 → today


# ---------------------------------------------------------------------------
# build_sidecar_updates
# ---------------------------------------------------------------------------


class TestBuildSidecarUpdates:
    def test_only_classified_entries_included(self):
        # An entry that was untouched (no version stamp) should be excluded
        # from the delta — even if it has a wayback_excerpt from before.
        targets = [
            {
                "name": "a.com",
                "wayback_excerpt": {"title": "A"},
                "snapshot_classifier_version": "v1",
            },
            {
                "name": "b.net",
                "wayback_excerpt": None,
                "snapshot_classifier_version": "v1",
            },
            {
                "name": "untouched.org",
                "wayback_excerpt": {"title": "untouched"},
                # no version → not built by this run
            },
        ]
        sidecar = cc.build_sidecar_updates(targets)
        assert sidecar == {"a.com": {"title": "A"}, "b.net": None}

    def test_skips_entries_without_name(self):
        targets = [{"snapshot_classifier_version": "v1"}]  # no name
        assert cc.build_sidecar_updates(targets) == {}

    def test_toxic_excerpt_preserved_for_forensics(self):
        # Toxic entries get evicted from daily-domains but their excerpt
        # belongs in the sidecar so future forensics can see what triggered.
        targets = [{
            "name": "bad.com",
            "wayback_excerpt": {"title": "XXX content"},
            "snapshot_category": "toxic",
            "snapshot_classifier_version": "v1",
        }]
        sidecar = cc.build_sidecar_updates(targets)
        assert sidecar["bad.com"] == {"title": "XXX content"}


# ---------------------------------------------------------------------------
# strip_inline_excerpts
# ---------------------------------------------------------------------------


class TestStripInlineExcerpts:
    def test_removes_wayback_excerpt_key(self):
        domains = [
            {"name": "a", "wayback_excerpt": {"title": "x"}, "score": 50},
            {"name": "b", "wayback_excerpt": None, "score": 40},
        ]
        cc.strip_inline_excerpts(domains)
        for d in domains:
            assert "wayback_excerpt" not in d
        # Other fields preserved
        assert domains[0]["name"] == "a"
        assert domains[0]["score"] == 50

    def test_idempotent_on_missing_key(self):
        domains = [{"name": "a", "score": 50}]
        cc.strip_inline_excerpts(domains)
        assert domains == [{"name": "a", "score": 50}]


# ---------------------------------------------------------------------------
# Commit-message body
# ---------------------------------------------------------------------------


class TestCommitMessage:
    def test_title_includes_date(self):
        title, _ = cc._build_commit_message(
            "summary", [], date(2026, 5, 18),
        )
        assert "2026-05-18" in title

    def test_body_lists_evicted_names_sorted(self):
        _, body = cc._build_commit_message(
            "summary line", ["zzz.com", "aaa.net"], date(2026, 5, 18),
        )
        # Sorted alphabetically for stable git log
        aaa_pos = body.index("aaa.net")
        zzz_pos = body.index("zzz.com")
        assert aaa_pos < zzz_pos
        assert "Evicted 2 toxic domain(s)" in body
        assert "summary line" in body

    def test_body_omits_eviction_block_when_no_evictions(self):
        _, body = cc._build_commit_message(
            "summary line", [], date(2026, 5, 18),
        )
        assert "Evicted" not in body
        assert body.strip() == "summary line"


# ---------------------------------------------------------------------------
# run() — end-to-end with mocked dependencies
# ---------------------------------------------------------------------------


class TestRunDryRun:
    def test_dry_run_writes_nothing(
        self, monkeypatch, sample_payload, tmp_paths, write_payload,
    ):
        write_payload(sample_payload)
        good_excerpt = {"title": "Real site"}
        _stub_fetch(monkeypatch, {"alpha.com": good_excerpt})
        client = _ScriptedClient(["legitimate"])

        # Mock _git so a stray call would explode the test
        monkeypatch.setattr(cc, "_git", MagicMock(side_effect=AssertionError(
            "_git must not be called in dry-run"
        )))

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=False,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: client,
        )

        assert rc == 0
        # File unchanged
        payload_after = json.loads(tmp_paths["daily"].read_text(encoding="utf-8"))
        assert payload_after == sample_payload
        # Sidecar not written
        assert not tmp_paths["sidecar"].exists()

    def test_dry_run_with_no_client_still_succeeds(
        self, sample_payload, tmp_paths, write_payload,
    ):
        write_payload(sample_payload)
        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=False,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: None,  # no key configured
        )
        assert rc == 0


class TestRunLive:
    def test_no_client_in_wet_run_aborts(
        self, sample_payload, tmp_paths, write_payload, caplog,
    ):
        write_payload(sample_payload)
        with caplog.at_level("ERROR"):
            rc = cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=False, no_push=False,
                today=date(2026, 5, 18),
                client_factory=lambda *_a, **_k: None,
            )
        assert rc == 1
        # Message changed with the 2026-09-18 backend switch: a missing API
        # key is no longer the failure mode (the classifier runs on a Claude
        # Code subscription token now), so the abort names the backend config
        # instead.
        assert any("No usable LLM backend" in m for m in caplog.messages)
        # File untouched
        payload_after = json.loads(tmp_paths["daily"].read_text(encoding="utf-8"))
        assert payload_after == sample_payload

    def test_full_run_evicts_toxic_writes_sidecar_commits(
        self, monkeypatch, sample_payload, tmp_paths, write_payload,
    ):
        write_payload(sample_payload)
        # Force=True picks all four. Charlie's snapshot date is set, others vary.
        good = {"title": "Legitimate content"}
        toxic = {"title": "XXX adult videos"}
        parked = {"title": "Buy this domain"}
        _stub_fetch(monkeypatch, {
            "alpha.com": good,
            "bravo.net": toxic,
            "charlie.org": parked,
            # delta.io has no wayback_last_snapshot → no fetch, → unknown
        })
        client = _ScriptedClient(["legitimate", "toxic", "parked"])

        # Stub git: record commands, simulate the relevant returncode +
        # stdout per command. `rev-parse HEAD` and `rev-parse origin/main`
        # return the same SHA so the post-push verification passes; the
        # silent-success-mismatch case has its own test below.
        git_calls: list[list[str]] = []
        SAME_SHA = "abc1234567890abcdef1234567890abcdef12345"
        def _fake_git(args, *, cwd=cc.REPO_ROOT, check=True):
            git_calls.append(args)
            result = MagicMock()
            if args[:2] == ["diff", "--cached"]:
                result.returncode = 1
                result.stdout = ""
            elif args[:1] == ["rev-parse"]:
                result.returncode = 0
                result.stdout = SAME_SHA + "\n"
            else:
                result.returncode = 0
                result.stdout = ""
            return result
        monkeypatch.setattr(cc, "_git", _fake_git)

        # Mock the bare-subprocess push call. Provide realistic stdout/
        # stderr so the new logging path has data to redact + emit.
        push_calls = []
        def _fake_push(args, *, cwd, capture_output, text):
            push_calls.append(args)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = (
                "To https://github.com/oiramix/domainsifter.git\n"
                "   abc1234..def5678  main -> main\n"
            )
            return r
        monkeypatch.setattr(cc.subprocess, "run", _fake_push)

        monkeypatch.setenv("GITHUB_TOKEN", "test-token-xyz")

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=True, only_unknown=False, limit=None,
            dry_run=False, no_push=False,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: client,
        )

        assert rc == 0
        # daily-domains.json: toxic bravo.net evicted, counts updated, no
        # wayback_excerpt inline on any entry.
        payload_after = json.loads(tmp_paths["daily"].read_text(encoding="utf-8"))
        names_after = [d["name"] for d in payload_after["domains"]]
        assert "bravo.net" not in names_after
        assert set(names_after) == {"alpha.com", "charlie.org", "delta.io"}
        assert payload_after["domain_count"] == 3
        for d in payload_after["domains"]:
            assert "wayback_excerpt" not in d
            # Constant, not a literal — bumped to v2 on the 2026-09-18
            # batching + backend switch. This test is about the stamp being
            # written, not about which version it names.
            assert d["snapshot_classifier_version"] == sc.CLASSIFIER_VERSION

        # Sidecar: contains all four (including evicted toxic), keyed by name.
        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        assert set(sidecar.keys()) == {"alpha.com", "bravo.net", "charlie.org", "delta.io"}
        assert sidecar["bravo.net"] == toxic  # toxic excerpt preserved for forensics
        assert sidecar["delta.io"] is None    # no snapshot → no excerpt

        # Git config + add + commit were called
        commands = [c[0] for c in git_calls]
        assert "config" in commands
        assert "add" in commands
        assert "commit" in commands

        # Push happened with token in URL argv
        assert len(push_calls) == 1
        push_argv = push_calls[0]
        assert push_argv[0] == "git" and push_argv[1] == "push"
        assert "test-token-xyz" in push_argv[2]
        assert push_argv[-1] == "main"

    def test_silent_success_push_detected(
        self, monkeypatch, sample_payload, tmp_paths, write_payload, caplog,
    ):
        """Regression test for the 2026-05-18 incident: git push returns 0
        but origin/main is not actually updated. The post-push verification
        must detect this and raise rather than logging 'Pushed commit:'
        and silently letting the trap discard the local commit."""
        write_payload(sample_payload)
        _stub_fetch(monkeypatch, {"alpha.com": {"title": "X"}})
        client = _ScriptedClient(["legitimate"])

        LOCAL_HEAD = "aaaaaaa1111111111111111111111111111aaaa"
        ORIGIN_HEAD = "bbbbbbb2222222222222222222222222222bbbb"

        def _fake_git(args, *, cwd=cc.REPO_ROOT, check=True):
            result = MagicMock()
            if args[:2] == ["diff", "--cached"]:
                result.returncode = 1
                result.stdout = ""
            elif args == ["rev-parse", "HEAD"]:
                result.returncode = 0
                result.stdout = LOCAL_HEAD + "\n"
            elif args == ["rev-parse", "origin/main"]:
                # The smoking gun: origin/main is at a DIFFERENT SHA than
                # local HEAD even though git push returned 0.
                result.returncode = 0
                result.stdout = ORIGIN_HEAD + "\n"
            else:
                result.returncode = 0
                result.stdout = ""
            return result
        monkeypatch.setattr(cc, "_git", _fake_git)

        def _fake_push(args, *, cwd, capture_output, text):
            r = MagicMock()
            r.returncode = 0  # PRETENDS success
            r.stdout = ""
            r.stderr = "Everything up-to-date\n"
            return r
        monkeypatch.setattr(cc.subprocess, "run", _fake_push)

        monkeypatch.setenv("GITHUB_TOKEN", "test-token-xyz")

        with caplog.at_level("ERROR"):
            rc = cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=False, no_push=False,
                today=date(2026, 5, 18),
                client_factory=lambda *_a, **_k: client,
            )
        assert rc == 2  # commit-and-push raised → run() returns 2
        # Error message should reference both SHAs so the operator can
        # see the divergence in the log.
        assert any(
            ORIGIN_HEAD[:7] in m and LOCAL_HEAD[:7] in m for m in caplog.messages
        )

    def test_push_failure_logs_sanitized_stderr(
        self, monkeypatch, sample_payload, tmp_paths, write_payload, caplog,
    ):
        """When git push fails non-zero, the actual stderr must surface
        in the log (sanitized to redact the token) so the operator can
        diagnose. The 2026-05-18 incident showed that swallowing stderr
        is dangerous — never repeat that."""
        write_payload(sample_payload)
        _stub_fetch(monkeypatch, {"alpha.com": {"title": "X"}})
        client = _ScriptedClient(["legitimate"])

        TOKEN = "ghp_supersecretxyz123"
        # Simulate a realistic git auth-failure stderr that echoes the URL.
        ERROR_STDERR = (
            f"remote: Permission to oiramix/domainsifter.git denied.\n"
            f"fatal: unable to access "
            f"'https://x-access-token:{TOKEN}@github.com/oiramix/domainsifter.git/': "
            f"The requested URL returned error: 403\n"
        )

        def _fake_git(args, *, cwd=cc.REPO_ROOT, check=True):
            result = MagicMock()
            if args[:2] == ["diff", "--cached"]:
                result.returncode = 1
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = ""
            return result
        monkeypatch.setattr(cc, "_git", _fake_git)

        def _fake_push(args, *, cwd, capture_output, text):
            r = MagicMock()
            r.returncode = 128
            r.stdout = ""
            r.stderr = ERROR_STDERR
            return r
        monkeypatch.setattr(cc.subprocess, "run", _fake_push)

        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)

        with caplog.at_level("INFO"):
            rc = cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=False, no_push=False,
                today=date(2026, 5, 18),
                client_factory=lambda *_a, **_k: client,
            )
        assert rc == 2
        # The stderr content (sanitized) must appear in logs
        all_log_text = "\n".join(caplog.messages)
        assert "Permission to oiramix/domainsifter.git denied" in all_log_text
        assert "403" in all_log_text
        # Token must NOT appear anywhere (verbatim or partial)
        assert TOKEN not in all_log_text
        # Redaction placeholder present
        assert "[REDACTED]" in all_log_text

    def test_no_push_skips_push(
        self, monkeypatch, sample_payload, tmp_paths, write_payload,
    ):
        write_payload(sample_payload)
        _stub_fetch(monkeypatch, {"alpha.com": {"title": "X"}})
        client = _ScriptedClient(["legitimate"])

        git_calls: list[list[str]] = []
        def _fake_git(args, *, cwd=cc.REPO_ROOT, check=True):
            git_calls.append(args)
            result = MagicMock()
            result.returncode = 1 if args[:2] == ["diff", "--cached"] else 0
            return result
        monkeypatch.setattr(cc, "_git", _fake_git)

        push_called = MagicMock()
        monkeypatch.setattr(cc.subprocess, "run", push_called)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=True,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: client,
        )

        assert rc == 0
        # Files written
        assert tmp_paths["daily"].exists()
        assert tmp_paths["sidecar"].exists()
        # No push call (subprocess.run never invoked)
        assert push_called.call_count == 0

    def test_empty_targets_returns_zero_without_write(
        self, monkeypatch, sample_payload, tmp_paths, write_payload,
    ):
        # All entries already have a snapshot_category. Default mode = no targets.
        for d in sample_payload["domains"]:
            d.setdefault("snapshot_category", "legitimate")
        write_payload(sample_payload)

        # Any write attempt should fail this test.
        monkeypatch.setattr(cc, "_atomic_write_json", MagicMock(
            side_effect=AssertionError("must not write when no targets"),
        ))

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=False,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: _ScriptedClient([]),
        )
        assert rc == 0

    def test_missing_daily_returns_error(self, tmp_paths):
        rc = cc.run(
            daily_path=tmp_paths["daily"],   # doesn't exist
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=True,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: None,
        )
        assert rc == 1

    def test_existing_sidecar_merged_not_replaced(
        self, monkeypatch, sample_payload, tmp_paths, write_payload,
    ):
        write_payload(sample_payload)
        # Pre-existing sidecar with an entry from a previous run.
        tmp_paths["sidecar"].write_text(
            json.dumps({"legacy.com": {"title": "Earlier"}}), encoding="utf-8",
        )
        _stub_fetch(monkeypatch, {"alpha.com": {"title": "New"}})
        client = _ScriptedClient(["legitimate"])

        # no_push=True below means _git is never called; a plain MagicMock
        # is fine here (the new rev-parse verification only runs inside
        # _git_commit_and_push, which is gated by no_push).
        monkeypatch.setattr(cc, "_git", MagicMock(return_value=MagicMock(returncode=1)))
        monkeypatch.setattr(cc.subprocess, "run", MagicMock(return_value=MagicMock(returncode=0)))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=True,
            today=date(2026, 5, 18),
            client_factory=lambda *_a, **_k: client,
        )

        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        # Legacy entry preserved AND new one added
        assert "legacy.com" in sidecar
        assert sidecar["legacy.com"] == {"title": "Earlier"}
        assert sidecar["alpha.com"] == {"title": "New"}


# ---------------------------------------------------------------------------
# Excerpt-cache pure helpers
# ---------------------------------------------------------------------------


class TestIsUsableExcerpt:
    def test_content_field_makes_it_usable(self):
        assert cc.is_usable_excerpt({"title": "Coppernest Pottery"})
        assert cc.is_usable_excerpt({"meta_description": "Hand-thrown mugs"})
        assert cc.is_usable_excerpt({"h1": ["Welcome"]})
        assert cc.is_usable_excerpt({"h2": ["Our kilns"]})

    def test_remembered_failure_is_not_usable(self):
        assert not cc.is_usable_excerpt(None)

    def test_bookkeeping_only_is_not_usable(self):
        # A fetch that resolved a snapshot URL but extracted no content is
        # not evidence — it must never displace a stored excerpt.
        assert not cc.is_usable_excerpt({
            "snapshot_url": "https://web.archive.org/web/2025/x",
            "snapshot_timestamp": "20250814120000",
            "title": None, "meta_description": "", "h1": [], "h2": [],
        })

    def test_non_dict_is_not_usable(self):
        assert not cc.is_usable_excerpt("Coppernest")
        assert not cc.is_usable_excerpt(["title"])


class TestReuseFlag:
    def test_default_is_on_when_key_absent(self):
        assert cc.reuse_cached_excerpts_enabled({}) is True
        assert cc.reuse_cached_excerpts_enabled(None) is True
        assert cc.reuse_cached_excerpts_enabled(
            {"snapshot_classifier": {"batch_size": 20}}
        ) is True

    def test_explicit_false_disables(self):
        assert cc.reuse_cached_excerpts_enabled(
            {"snapshot_classifier": {"reuse_cached_excerpts": False}}
        ) is False

    def test_malformed_section_falls_back_to_default(self):
        assert cc.reuse_cached_excerpts_enabled(
            {"snapshot_classifier": "not-a-dict"}
        ) is True


class TestDescribeExcerptAgeing:
    """The fragment that stops an operator misreading a low "reused" count:
    the classifier ages excerpts by CAPTURE date, and dropped domains' last
    captures are mostly old, so most cached entries get re-fetched even
    though reuse is working."""

    def test_positive_limit_is_named(self):
        text = cc._describe_excerpt_ageing(
            {"snapshot_classifier": {"excerpt_max_age_days": 90}}
        )
        assert "excerpt_max_age_days=90" in text

    def test_zero_means_no_ageing(self):
        text = cc._describe_excerpt_ageing(
            {"snapshot_classifier": {"excerpt_max_age_days": 0}}
        )
        assert "no age limit" in text

    def test_absent_or_malformed_says_nothing(self):
        assert cc._describe_excerpt_ageing({}) == ""
        assert cc._describe_excerpt_ageing(None) == ""
        assert cc._describe_excerpt_ageing({"snapshot_classifier": {}}) == ""
        assert cc._describe_excerpt_ageing(
            {"snapshot_classifier": {"excerpt_max_age_days": "ninety"}}
        ) == ""


class TestMergeSidecar:
    def test_failed_refetch_never_overwrites_stored_excerpt(self):
        """THE regression this change exists to prevent: on 2026-09-21 the
        sidecar held 598 entries of which 214 had content; a plain
        {**existing, **updates} merge on a run whose fetches failed would
        replace those with null and re-expose screened domains."""
        existing = {"coppernest.org": STORED_EXCERPT}
        merged = cc.merge_sidecar(existing, {"coppernest.org": None})
        assert merged["coppernest.org"] == STORED_EXCERPT

    def test_usable_new_excerpt_replaces_stored(self):
        fresh = {"title": "Coppernest Pottery — new capture"}
        merged = cc.merge_sidecar(
            {"coppernest.org": STORED_EXCERPT}, {"coppernest.org": fresh},
        )
        assert merged["coppernest.org"] == fresh

    def test_unusable_new_excerpt_does_not_overwrite_stored(self):
        # A dict that fetched but extracted nothing is as bad as None.
        merged = cc.merge_sidecar(
            {"coppernest.org": STORED_EXCERPT},
            {"coppernest.org": {"title": None, "h1": []}},
        )
        assert merged["coppernest.org"] == STORED_EXCERPT

    def test_first_failure_is_recorded_as_null(self):
        # Unchanged from the old behaviour: with nothing stored, a failure
        # still lands in the sidecar as null.
        merged = cc.merge_sidecar({}, {"tideblock.io": None})
        assert merged["tideblock.io"] is None

    def test_stored_null_is_replaced_by_content(self):
        merged = cc.merge_sidecar(
            {"tideblock.io": None}, {"tideblock.io": {"title": "Tideblock"}},
        )
        assert merged["tideblock.io"] == {"title": "Tideblock"}

    def test_domains_absent_from_updates_are_untouched(self):
        existing = {
            "driftlantern.net": UNRELATED_EXCERPT,
            "marketglow.com": None,
        }
        merged = cc.merge_sidecar(existing, {"coppernest.org": None})
        assert merged["driftlantern.net"] == UNRELATED_EXCERPT
        assert merged["marketglow.com"] is None
        assert set(merged) == {
            "driftlantern.net", "marketglow.com", "coppernest.org",
        }

    def test_does_not_mutate_the_input(self):
        existing = {"coppernest.org": STORED_EXCERPT}
        cc.merge_sidecar(existing, {"tideblock.io": None})
        assert existing == {"coppernest.org": STORED_EXCERPT}


class TestCountExcerptSources:
    def test_splits_reused_fetched_and_missing(self):
        cache = {
            "coppernest.org": STORED_EXCERPT,
            "tideblock.io": {"title": "Old Tideblock"},
        }
        targets = [
            # byte-identical to the cache → reused, no archive.org hit
            {"name": "coppernest.org", "wayback_excerpt": dict(STORED_EXCERPT)},
            # different content → freshly fetched
            {"name": "tideblock.io", "wayback_excerpt": {"title": "New Tideblock"}},
            # nothing at all → no usable excerpt
            {"name": "marketglow.com", "wayback_excerpt": None},
        ]
        assert cc.count_excerpt_sources(targets, cache) == {
            "reused": 1, "fetched": 1, "missing": 1,
        }

    def test_no_cache_counts_everything_as_fetched(self):
        targets = [{"name": "coppernest.org", "wayback_excerpt": STORED_EXCERPT}]
        assert cc.count_excerpt_sources(targets, None) == {
            "reused": 0, "fetched": 1, "missing": 0,
        }


class TestAcceptsExcerptCache:
    def test_true_for_the_new_signature(self):
        def new_sig(candidates, *, client=None, config=None, excerpt_cache=None):
            return {}
        assert cc._accepts_excerpt_cache(new_sig) is True

    def test_false_for_the_old_signature(self):
        assert cc._accepts_excerpt_cache(_legacy_classify_all) is False

    def test_true_for_kwargs_passthrough(self):
        assert cc._accepts_excerpt_cache(_ClassifyAllSpy()) is True

    def test_real_classifier_is_called_correctly_either_way(self):
        # Whatever the concurrently-edited classifier currently looks like,
        # the answer must be a bool and must not raise.
        assert isinstance(
            cc._accepts_excerpt_cache(sc.classify_all), bool
        )


# ---------------------------------------------------------------------------
# run() — excerpt reuse end to end
# ---------------------------------------------------------------------------


class TestRunExcerptReuse:
    def test_sidecar_is_passed_in_as_excerpt_cache(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        write_payload(reuse_payload)
        stored = {"coppernest.org": STORED_EXCERPT, "tideblock.io": None}
        write_sidecar(stored)
        spy = _ClassifyAllSpy(
            excerpts={"coppernest.org": STORED_EXCERPT},
            categories={"coppernest.org": "legitimate"},
        )
        monkeypatch.setattr(sc, "classify_all", spy)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(True),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        assert rc == 0
        assert spy.last_kwargs["excerpt_cache"] == stored
        # A copy, not the object we merge into later.
        assert spy.last_kwargs["excerpt_cache"] is not stored

    def test_reuse_benefit_logged_once(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload,
        write_sidecar, caplog,
    ):
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        spy = _ClassifyAllSpy(
            excerpts={
                # reused verbatim from the cache
                "coppernest.org": dict(STORED_EXCERPT),
                # freshly fetched
                "tideblock.io": {"title": "Tideblock Surf Report"},
            },
            categories={
                "coppernest.org": "legitimate", "tideblock.io": "legitimate",
            },
        )
        monkeypatch.setattr(sc, "classify_all", spy)

        with caplog.at_level("INFO"):
            cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=True, no_push=True,
                today=date(2026, 9, 21),
                config=_reuse_config(True),
                client_factory=lambda *_a, **_k: MagicMock(),
            )

        reuse_lines = [m for m in caplog.messages if "excerpt sources:" in m]
        assert len(reuse_lines) == 1          # one summary line, not per domain
        assert "1 reused from sidecar" in reuse_lines[0]
        assert "1 fetched from archive.org" in reuse_lines[0]

    def test_stored_excerpt_survives_a_failed_refetch(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        """The merge contract: a usable stored excerpt + a fetch that failed
        this run → the stored excerpt is still in the sidecar afterwards, and
        an unrelated domain's entry is untouched."""
        write_payload(reuse_payload)
        write_sidecar({
            "coppernest.org": STORED_EXCERPT,
            "driftlantern.net": UNRELATED_EXCERPT,   # not in this run at all
        })
        # Both of this run's targets come back with NO excerpt (archive.org
        # down), which in the old code path wrote null over coppernest.org.
        spy = _ClassifyAllSpy(excerpts={
            "coppernest.org": None, "tideblock.io": None,
        })
        monkeypatch.setattr(sc, "classify_all", spy)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(True),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        assert rc == 0
        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        assert sidecar["coppernest.org"] == STORED_EXCERPT   # survived
        assert sidecar["driftlantern.net"] == UNRELATED_EXCERPT  # untouched
        assert sidecar["tideblock.io"] is None   # first failure remembered

    def test_reuse_disabled_passes_no_cache(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        spy = _ClassifyAllSpy(excerpts={"coppernest.org": STORED_EXCERPT})
        monkeypatch.setattr(sc, "classify_all", spy)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(False),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        assert rc == 0
        # Today's behaviour exactly: the kwarg is absent, not empty.
        assert "excerpt_cache" not in spy.last_kwargs

    def test_reuse_disabled_still_merges_protectively(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        # Reuse off only stops the cache being READ. The merge still must not
        # destroy stored content — that guard is not behind the flag.
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        monkeypatch.setattr(sc, "classify_all", _ClassifyAllSpy(excerpts={
            "coppernest.org": None, "tideblock.io": None,
        }))

        cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(False),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        assert sidecar["coppernest.org"] == STORED_EXCERPT

    def test_dry_run_does_not_touch_the_sidecar(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        before = tmp_paths["sidecar"].read_text(encoding="utf-8")
        monkeypatch.setattr(sc, "classify_all", _ClassifyAllSpy(excerpts={
            "coppernest.org": None, "tideblock.io": {"title": "New"},
        }))
        monkeypatch.setattr(cc, "_atomic_write_json", MagicMock(
            side_effect=AssertionError("dry run must write nothing"),
        ))

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=False,
            today=date(2026, 9, 21),
            config=_reuse_config(True),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        assert rc == 0
        assert tmp_paths["sidecar"].read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("content", ["{not json", '["a-list"]', '"a string"'])
    def test_corrupt_sidecar_degrades_to_no_cache(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, content,
    ):
        """Hard rule 17: an unreadable sidecar costs us the reuse benefit for
        the run, it does not raise and it does not abort."""
        write_payload(reuse_payload)
        tmp_paths["sidecar"].write_text(content, encoding="utf-8")
        spy = _ClassifyAllSpy(excerpts={
            "coppernest.org": {"title": "Coppernest"},
        })
        monkeypatch.setattr(sc, "classify_all", spy)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(True),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        assert rc == 0
        # Empty cache → nothing to pass, so the kwarg is omitted entirely.
        assert "excerpt_cache" not in spy.last_kwargs
        # And the rewrite leaves a valid sidecar holding this run's data.
        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        assert sidecar["coppernest.org"] == {"title": "Coppernest"}

    def test_missing_sidecar_is_not_an_error(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload,
    ):
        write_payload(reuse_payload)
        assert not tmp_paths["sidecar"].exists()
        spy = _ClassifyAllSpy()
        monkeypatch.setattr(sc, "classify_all", spy)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=True, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(True),
            client_factory=lambda *_a, **_k: MagicMock(),
        )
        assert rc == 0
        assert "excerpt_cache" not in spy.last_kwargs


class TestReuseSkipsTheFetch:
    def test_cached_domain_is_not_re_fetched(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        """Models the classifier side of the contract: a usable cached excerpt
        is reused and the archive.org call is skipped, a cached null still
        retries. Pins that the cache we hand over is keyed and shaped the way
        the classifier expects."""
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT, "tideblock.io": None})
        fetched: list[str] = []

        def _cache_aware_classify_all(
            candidates, *, client=None, pause_seconds=1.0, config=None,
            excerpt_cache=None,
        ):
            cache = excerpt_cache or {}
            counts = {c: 0 for c in (*sc.VALID_CATEGORIES, sc.UNKNOWN_CATEGORY)}
            for record in candidates:
                name = record.get("name", "")
                cached = cache.get(name)
                if cc.is_usable_excerpt(cached):
                    excerpt = cached
                else:
                    fetched.append(name)
                    excerpt = {"title": f"fresh {name}"}
                record["wayback_excerpt"] = excerpt
                record["snapshot_category"] = "legitimate"
                record["snapshot_classifier_version"] = sc.CLASSIFIER_VERSION
                counts["legitimate"] += 1
            return counts

        monkeypatch.setattr(sc, "classify_all", _cache_aware_classify_all)

        rc = cc.run(
            daily_path=tmp_paths["daily"],
            excerpts_path=tmp_paths["sidecar"],
            force=False, only_unknown=False, limit=None,
            dry_run=False, no_push=True,
            today=date(2026, 9, 21),
            config=_reuse_config(True),
            client_factory=lambda *_a, **_k: MagicMock(),
        )

        assert rc == 0
        # coppernest.org had content stored → no archive.org hit. tideblock.io
        # had a remembered null → retried.
        assert fetched == ["tideblock.io"]
        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        assert sidecar["coppernest.org"] == STORED_EXCERPT
        assert sidecar["tideblock.io"] == {"title": "fresh tideblock.io"}


class TestRunAgainstOlderClassifier:
    """The classifier's excerpt_cache kwarg may land AFTER this change. Both
    guards — signature introspection and the TypeError fallback — must keep
    the run working, just without the reuse benefit."""

    def test_old_signature_is_never_handed_the_kwarg(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload,
        write_sidecar, caplog,
    ):
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        _legacy_classify_all.calls.clear()
        monkeypatch.setattr(sc, "classify_all", _legacy_classify_all)

        with caplog.at_level("WARNING"):
            rc = cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=False, no_push=True,
                today=date(2026, 9, 21),
                config=_reuse_config(True),
                client_factory=lambda *_a, **_k: MagicMock(),
            )

        assert rc == 0
        # Called exactly once — no TypeError, so no retry, so no duplicate
        # classification cost.
        assert _legacy_classify_all.calls == [2]
        assert any("older build" in m for m in caplog.messages)

    def test_typeerror_naming_the_kwarg_retries_without_it(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload,
        write_sidecar, caplog,
    ):
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        spy = _ClassifyAllSpy(
            excerpts={"coppernest.org": STORED_EXCERPT},
            raise_on_cache=TypeError(
                "classify_all() got an unexpected keyword argument "
                "'excerpt_cache'"
            ),
        )
        monkeypatch.setattr(sc, "classify_all", spy)

        with caplog.at_level("WARNING"):
            rc = cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=False, no_push=True,
                today=date(2026, 9, 21),
                config=_reuse_config(True),
                client_factory=lambda *_a, **_k: MagicMock(),
            )

        assert rc == 0
        assert len(spy.calls) == 2                       # rejected, then retried
        assert "excerpt_cache" not in spy.calls[1]
        assert any("retrying without reuse" in m for m in caplog.messages)
        # The stored excerpt still survived the cache-less run.
        sidecar = json.loads(tmp_paths["sidecar"].read_text(encoding="utf-8"))
        assert sidecar["coppernest.org"] == STORED_EXCERPT

    def test_unrelated_typeerror_is_not_swallowed(
        self, monkeypatch, reuse_payload, tmp_paths, write_payload, write_sidecar,
    ):
        # A TypeError from inside the classifier must NOT trigger a blind
        # re-run of a whole classification pass.
        write_payload(reuse_payload)
        write_sidecar({"coppernest.org": STORED_EXCERPT})
        spy = _ClassifyAllSpy(
            raise_on_cache=TypeError("'<' not supported between int and str"),
        )
        monkeypatch.setattr(sc, "classify_all", spy)

        with pytest.raises(TypeError):
            cc.run(
                daily_path=tmp_paths["daily"],
                excerpts_path=tmp_paths["sidecar"],
                force=False, only_unknown=False, limit=None,
                dry_run=True, no_push=True,
                today=date(2026, 9, 21),
                config=_reuse_config(True),
                client_factory=lambda *_a, **_k: MagicMock(),
            )
        assert len(spy.calls) == 1   # no retry


# ---------------------------------------------------------------------------
# CLI argument validation
# ---------------------------------------------------------------------------


class TestCli:
    def test_force_plus_only_unknown_rejected(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cc.main(["--force", "--only-unknown"])
        # argparse parser.error → exit 2
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "mutually exclusive" in captured.err
