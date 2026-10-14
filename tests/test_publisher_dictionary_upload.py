"""Tests for the dictionary upload (src/publisher/dictionary/upload.py), the two
PoolApiClient calls it makes, and the nightly entry point that strings crawl,
export and upload together.

Offline: the API client is a MagicMock or runs over a MagicMock session, the
crawl is replaced, and the autouse `_no_sockets` guard fails any test that
reaches for the network.
"""
import datetime as dt
import logging
import socket
from unittest.mock import MagicMock, call

import pytest

from src.publisher.client import PoolApiClient, PoolApiError
from src.publisher.dictionary import __main__ as entry
from src.publisher.dictionary import crawl, export, upload


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def _guard(*args, **kwargs):
        raise AssertionError("test attempted to open a real socket")

    monkeypatch.setattr(socket, "socket", _guard)


NOW = dt.datetime(2026, 10, 14, 5, 40, tzinfo=dt.timezone.utc)


def _export_dir(tmp_path, shards=3):
    entries = [[f"artist {i}", 1, "2026-10", 1] for i in range(shards)]
    groups = [[entry] for entry in entries]
    built = export.Export(
        version="2026-10-14T05:40Z",
        shards=[{"schema_version": 1, "version": "2026-10-14T05:40Z", "index": i,
                 "total": shards, "entries": group} for i, group in enumerate(groups)],
        commit={"schema_version": 1, "version": "2026-10-14T05:40Z", "total": shards,
                "entry_count": shards, "crawled_through": "2016-11"},
    )
    out = str(tmp_path / "export")
    export.write_export(built, out)
    return out, built


def _api():
    api = MagicMock()
    api.post_dictionary_shard.side_effect = lambda payload: {
        "slot": "b", "index": payload["index"], "stored": len(payload["entries"])}
    api.post_dictionary_commit.return_value = {"version": "2026-10-14T05:40Z"}
    return api


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def test_upload_commits_after_all_shards(tmp_path):
    out, built = _export_dir(tmp_path, shards=3)
    api = _api()

    outcome = upload.upload(api, out, "dev")

    assert api.mock_calls == [
        call.post_dictionary_shard(built.shards[0]),
        call.post_dictionary_shard(built.shards[1]),
        call.post_dictionary_shard(built.shards[2]),
        call.post_dictionary_commit(built.commit),
    ]
    assert outcome.ok and outcome.committed
    assert (outcome.shards_posted, outcome.total, outcome.stored) == (3, 3, 3)


def test_upload_failure_before_commit_never_commits(tmp_path):
    out, _ = _export_dir(tmp_path, shards=3)
    api = _api()
    api.post_dictionary_shard.side_effect = [
        {"stored": 1}, PoolApiError(503, "transport", "503"), {"stored": 1}]

    outcome = upload.upload(api, out, "dev")

    api.post_dictionary_commit.assert_not_called()
    assert api.post_dictionary_shard.call_count == 2      # stops at the failure
    assert not outcome.ok and outcome.committed is False
    assert outcome.error == "status=503 error=transport detail=503"


def test_upload_refuses_a_shard_from_another_version(tmp_path):
    out, built = _export_dir(tmp_path, shards=2)
    stale = dict(built.shards[1], version="2026-10-13T05:40Z")
    export.write_export(export.Export(built.version, [built.shards[0], stale], built.commit), out)
    api = _api()

    outcome = upload.upload(api, out, "dev")

    api.post_dictionary_commit.assert_not_called()
    assert not outcome.ok and "does not belong" in outcome.error


def test_upload_to_env_uses_the_publishers_per_env_settings(tmp_path, monkeypatch):
    out, _ = _export_dir(tmp_path, shards=1)
    monkeypatch.setenv("TUNEFINDER_POOL_API_DEV", "https://dev.example")
    monkeypatch.delenv("TUNEFINDER_POOL_API_PROD", raising=False)
    from src.config import load_settings

    settings = load_settings()
    api = _api()
    factory = MagicMock(return_value=api)

    dev = upload.upload_to_env(settings, "dev", out, client_factory=factory)
    prod = upload.upload_to_env(settings, "prod", out, client_factory=factory)

    factory.assert_called_once_with(settings, "dev")
    assert dev.ok
    assert not prod.ok and prod.error == "no API url configured for prod"


# ---------------------------------------------------------------------------
# PoolApiClient's two dictionary calls
# ---------------------------------------------------------------------------

def _resp(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    return resp


def test_client_posts_dictionary_shard_and_commit_with_retry():
    tokens = MagicMock()
    tokens.token.return_value = "tok-1"
    session = MagicMock()
    session.request.side_effect = [
        _resp(429),
        _resp(200, {"slot": "a", "index": 0, "stored": 1}),
        _resp(200, {"version": "v"}),
    ]
    sleep = MagicMock()
    client = PoolApiClient("https://api.example/", tokens, session=session, sleep=sleep)
    shard = {"schema_version": 1, "version": "v", "index": 0, "total": 1, "entries": [["A", 1, "2026-10", 1]]}
    commit = {"schema_version": 1, "version": "v", "total": 1, "entry_count": 1, "crawled_through": "2026-10"}

    assert client.post_dictionary_shard(shard) == {"slot": "a", "index": 0, "stored": 1}
    assert client.post_dictionary_commit(commit) == {"version": "v"}

    urls = [(c.args[0], c.args[1]) for c in session.request.call_args_list]
    assert urls == [
        ("POST", "https://api.example/api/ingest/dictionary/shard"),
        ("POST", "https://api.example/api/ingest/dictionary/shard"),
        ("POST", "https://api.example/api/ingest/dictionary/commit"),
    ]
    assert session.request.call_args_list[1].kwargs["json"] == shard
    assert session.request.call_args_list[2].kwargs["json"] == commit
    assert session.request.call_args_list[2].kwargs["headers"]["Authorization"] == "Bearer tok-1"
    sleep.assert_called_once_with(5)                      # the 429, on the publisher's backoff


def test_client_dictionary_commit_refusal_is_a_pool_api_error():
    tokens = MagicMock()
    tokens.token.return_value = "tok-1"
    session = MagicMock()
    session.request.return_value = _resp(409, {"error": "incomplete_version", "detail": "shard 3"})
    client = PoolApiClient("https://api.example", tokens, session=session, sleep=MagicMock())

    with pytest.raises(PoolApiError) as raised:
        client.post_dictionary_commit({"version": "v"})
    assert (raised.value.status, raised.value.error) == (409, "incomplete_version")


# ---------------------------------------------------------------------------
# nightly
# ---------------------------------------------------------------------------

@pytest.fixture
def nightly_env(tmp_path, monkeypatch):
    """A database with one crawled credit; the crawl itself and the upload faked."""
    db_path = str(tmp_path / "crawl.db")
    db = crawl.connect(db_path)
    db.execute("INSERT INTO artists VALUES (1, 'Burial', 'burial')")
    db.execute("INSERT INTO tracks VALUES (100, 9, '2024-06-01')")
    db.execute("INSERT INTO credits VALUES (100, 1, 'artist')")
    db.commit()
    db.close()

    phases = []

    def fake_run_crawl(db, make_client, deadline=None, top_up_when_done=False, **_):
        phases.append(("crawl", top_up_when_done))
        return crawl.CrawlOutcome(requests=0, note="finished", topped_up=False, error=False)

    def fake_build_export(db, taxonomy=None, now=None):
        phases.append(("export",))
        return real_build_export(db, now=NOW)

    real_build_export = export.build_export
    monkeypatch.setattr(entry, "setup_logging", lambda: None)
    monkeypatch.setattr(crawl, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(export, "build_export", fake_build_export)

    def set_upload(ok):
        def fake_upload_to_env(settings, env, export_dir, client_factory=None):
            phases.append(("upload", env))
            outcome = upload.UploadOutcome(env=env, version="2026-10-14T05:40Z", total=1)
            if ok:
                outcome.shards_posted, outcome.stored, outcome.committed = 1, 1, True
            else:
                outcome.error = "status=503 error=transport detail=503"
            return outcome

        monkeypatch.setattr(upload, "upload_to_env", fake_upload_to_env)

    return db_path, phases, set_upload


def test_nightly_runs_crawl_export_upload_in_order(nightly_env, caplog):
    db_path, phases, set_upload = nightly_env
    set_upload(ok=True)

    with caplog.at_level(logging.INFO):
        code = entry.main(["--db", db_path, "nightly", "--env", "dev", "--until", "05:30"])

    assert code == 0
    assert phases == [("crawl", True), ("export",), ("upload", "dev")]
    lines = [r.getMessage() for r in caplog.records if r.name == entry.__name__]
    assert [line.split(":")[0] for line in lines] == [
        "[dictionary] crawl", "[dictionary] export", "[dictionary] upload dev"]


def test_nightly_exits_nonzero_on_upload_failure(nightly_env):
    db_path, phases, set_upload = nightly_env
    set_upload(ok=False)

    code = entry.main(["--db", db_path, "nightly", "--env", "dev"])

    assert code == 1
    assert phases[-1] == ("upload", "dev")
