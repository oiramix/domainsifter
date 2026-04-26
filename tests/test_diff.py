"""Unit tests for scripts/diff.py — uses tmp_path for state directories."""

from __future__ import annotations

from scripts import diff


def test_load_yesterday_returns_empty_on_cold_start(tmp_path):
    assert diff.load_yesterday(tmp_path, "app") == set()


def test_commit_today_writes_sorted_one_per_line(tmp_path):
    written = diff.commit_today(tmp_path, "app", {"zeta.app", "alpha.app", "beta.app"})
    assert written.read_text(encoding="utf-8") == "alpha.app\nbeta.app\nzeta.app\n"


def test_commit_today_creates_state_dir_if_missing(tmp_path):
    nested = tmp_path / "nested" / "state"
    diff.commit_today(nested, "app", {"foo.app"})
    assert (nested / "app_yesterday.txt").exists()


def test_commit_today_handles_empty_set(tmp_path):
    written = diff.commit_today(tmp_path, "app", set())
    assert written.read_text(encoding="utf-8") == ""


def test_load_yesterday_roundtrips_with_commit(tmp_path):
    today = {"a.app", "b.app", "c.app"}
    diff.commit_today(tmp_path, "app", today)
    assert diff.load_yesterday(tmp_path, "app") == today


def test_load_yesterday_skips_blank_lines(tmp_path):
    path = tmp_path / "app_yesterday.txt"
    path.write_text("a.app\n\nb.app\n   \nc.app\n", encoding="utf-8")
    assert diff.load_yesterday(tmp_path, "app") == {"a.app", "b.app", "c.app"}


def test_compute_drops_returns_yesterday_minus_today():
    yesterday = {"keeper.app", "dropped1.app", "dropped2.app"}
    today = {"keeper.app", "newcomer.app"}
    assert diff.compute_drops(yesterday, today) == {"dropped1.app", "dropped2.app"}


def test_compute_drops_empty_when_today_is_superset():
    assert diff.compute_drops({"a.app"}, {"a.app", "b.app"}) == set()


def test_compute_drops_returns_all_yesterday_when_today_empty():
    assert diff.compute_drops({"a.app", "b.app"}, set()) == {"a.app", "b.app"}


def test_diff_and_commit_cold_start_returns_empty_and_writes_today(tmp_path):
    today = {"a.app", "b.app"}
    drops = diff.diff_and_commit(tmp_path, "app", today)
    assert drops == set()
    assert diff.load_yesterday(tmp_path, "app") == today


def test_diff_and_commit_warm_run_detects_drops(tmp_path):
    diff.commit_today(tmp_path, "app", {"keeper.app", "dropping.app"})
    drops = diff.diff_and_commit(tmp_path, "app", {"keeper.app", "newcomer.app"})
    assert drops == {"dropping.app"}
    assert diff.load_yesterday(tmp_path, "app") == {"keeper.app", "newcomer.app"}


def test_tld_lowercased_in_path(tmp_path):
    diff.commit_today(tmp_path, "APP", {"x.app"})
    assert (tmp_path / "app_yesterday.txt").exists()
