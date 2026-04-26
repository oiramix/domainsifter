"""Unit tests for scripts/diff.py — R2-backed state.

The R2 client is mocked via unittest.mock per the project pattern (CLAUDE.md
rule #13: tests never hit live APIs). Each test injects a fake S3 client
through the `client=` parameter; cold start is simulated by raising
`ClientError` with a NoSuchKey code from `get_object`.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from scripts import diff


BUCKET = "test-bucket"


def _no_such_key() -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
        "GetObject",
    )


def _make_client(body: bytes | None = None, raise_not_found: bool = False) -> MagicMock:
    """Build a fake S3 client. If `raise_not_found`, get_object raises a
    NoSuchKey ClientError; otherwise it returns the supplied body."""
    client = MagicMock()
    if raise_not_found:
        client.get_object.side_effect = _no_such_key()
    else:
        client.get_object.return_value = {"Body": BytesIO(body or b"")}
    return client


# --- _object_key --------------------------------------------------------------


def test_object_key_uses_state_prefix_and_lowercases_tld():
    assert diff._object_key("APP") == "state/app_yesterday.txt"
    assert diff._object_key("org") == "state/org_yesterday.txt"


# --- load_yesterday -----------------------------------------------------------


def test_load_yesterday_returns_empty_on_cold_start():
    client = _make_client(raise_not_found=True)
    assert diff.load_yesterday("app", client=client, bucket=BUCKET) == set()
    client.get_object.assert_called_once_with(Bucket=BUCKET, Key="state/app_yesterday.txt")


def test_load_yesterday_parses_lines_into_set():
    client = _make_client(b"alpha.app\nbeta.app\nzeta.app\n")
    assert diff.load_yesterday("app", client=client, bucket=BUCKET) == {
        "alpha.app", "beta.app", "zeta.app",
    }


def test_load_yesterday_skips_blank_lines():
    client = _make_client(b"a.app\n\nb.app\n   \nc.app\n")
    assert diff.load_yesterday("app", client=client, bucket=BUCKET) == {
        "a.app", "b.app", "c.app",
    }


def test_load_yesterday_propagates_non_404_errors():
    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}},
        "GetObject",
    )
    with pytest.raises(ClientError):
        diff.load_yesterday("app", client=client, bucket=BUCKET)


def test_load_yesterday_treats_404_code_as_cold_start():
    """Some R2 deployments surface raw '404' instead of NoSuchKey."""
    client = MagicMock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "GetObject",
    )
    assert diff.load_yesterday("app", client=client, bucket=BUCKET) == set()


# --- compute_drops ------------------------------------------------------------


def test_compute_drops_returns_yesterday_minus_today():
    yesterday = {"keeper.app", "dropped1.app", "dropped2.app"}
    today = {"keeper.app", "newcomer.app"}
    assert diff.compute_drops(yesterday, today) == {"dropped1.app", "dropped2.app"}


def test_compute_drops_empty_when_today_is_superset():
    assert diff.compute_drops({"a.app"}, {"a.app", "b.app"}) == set()


def test_compute_drops_returns_all_yesterday_when_today_empty():
    assert diff.compute_drops({"a.app", "b.app"}, set()) == {"a.app", "b.app"}


# --- commit_today -------------------------------------------------------------


def test_commit_today_writes_sorted_one_per_line():
    client = MagicMock()
    diff.commit_today(
        "app",
        {"zeta.app", "alpha.app", "beta.app"},
        client=client,
        bucket=BUCKET,
    )
    args, kwargs = client.put_object.call_args
    assert kwargs["Bucket"] == BUCKET
    assert kwargs["Key"] == "state/app_yesterday.txt"
    assert kwargs["Body"] == b"alpha.app\nbeta.app\nzeta.app\n"
    assert kwargs["ContentType"].startswith("text/plain")


def test_commit_today_handles_empty_set():
    client = MagicMock()
    diff.commit_today("app", set(), client=client, bucket=BUCKET)
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Body"] == b""


def test_commit_today_lowercases_tld_in_key():
    client = MagicMock()
    diff.commit_today("APP", {"x.app"}, client=client, bucket=BUCKET)
    assert client.put_object.call_args.kwargs["Key"] == "state/app_yesterday.txt"


def test_commit_today_returns_key():
    client = MagicMock()
    key = diff.commit_today("org", {"foo.org"}, client=client, bucket=BUCKET)
    assert key == "state/org_yesterday.txt"


# --- diff_and_commit ----------------------------------------------------------


def test_diff_and_commit_cold_start_returns_empty_and_writes_today():
    client = _make_client(raise_not_found=True)
    today = {"a.app", "b.app"}
    drops = diff.diff_and_commit("app", today, client=client, bucket=BUCKET)
    assert drops == set()
    # And we did write today's snapshot for tomorrow's run.
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Body"] == b"a.app\nb.app\n"


def test_diff_and_commit_warm_run_detects_drops():
    client = _make_client(b"keeper.app\ndropping.app\n")
    drops = diff.diff_and_commit(
        "app",
        {"keeper.app", "newcomer.app"},
        client=client,
        bucket=BUCKET,
    )
    assert drops == {"dropping.app"}
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Body"] == b"keeper.app\nnewcomer.app\n"


# --- _r2_client ---------------------------------------------------------------


def test_r2_client_uses_account_specific_endpoint(monkeypatch):
    """The boto3 client must point at the account-scoped R2 endpoint and
    use the credentials from env. We assert on the kwargs passed to
    boto3.client; we don't actually open a connection."""
    monkeypatch.setenv("R2_ACCOUNT_ID", "abc123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "SK")

    captured = {}

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(diff.boto3, "client", fake_client)
    diff._r2_client()

    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://abc123.r2.cloudflarestorage.com"
    assert captured["aws_access_key_id"] == "AK"
    assert captured["aws_secret_access_key"] == "SK"
    assert captured["region_name"] == "auto"
