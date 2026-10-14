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
from src.publisher.taxonomy import load_taxonomy


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
    api.get_config.return_value = {"taxonomy_version": load_taxonomy().version}
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
    db.execute("INSERT INTO artists (id, name, slug) VALUES (1, 'Burial', 'burial')")
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
    monkeypatch.setattr(entry, "_now", lambda: dt.datetime(2026, 10, 14, 21, 30))
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
        code = entry.main(["--db", db_path, "nightly", "--env", "dev", "--until", "05:15"])

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


def test_client_retries_a_500_on_the_dictionary_routes_only():
    tokens = MagicMock()
    tokens.token.return_value = "tok-1"
    session = MagicMock()
    session.request.side_effect = [_resp(500), _resp(200, {"stored": 1}),
                                   _resp(500), _resp(500), _resp(500), _resp(500)]
    sleep = MagicMock()
    client = PoolApiClient("https://api.example", tokens, session=session, sleep=sleep)

    assert client.post_dictionary_shard({"index": 0}) == {"stored": 1}
    with pytest.raises(PoolApiError) as raised:              # bounded: four attempts
        client.post_dictionary_commit({"version": "v"})
    assert (raised.value.status, raised.value.error) == (500, "transport")
    assert session.request.call_count == 6
    assert [c.args[0] for c in sleep.call_args_list] == [5, 5, 15, 45]

    session.request.side_effect = [_resp(500)]
    with pytest.raises(PoolApiError) as raised:              # the pool's routes: unchanged
        client.post_manifest({})
    assert (raised.value.status, raised.value.error) == (500, "unexpected_status")


def test_nightly_refuses_a_deadline_more_than_nine_hours_away(nightly_env, monkeypatch):
    db_path, phases, set_upload = nightly_env
    set_upload(ok=True)
    monkeypatch.setattr(entry, "_now", lambda: dt.datetime(2026, 10, 14, 14, 0))

    assert entry.main(["--db", db_path, "nightly", "--env", "dev", "--until", "05:15"]) == 2
    assert phases == []                                     # nothing ran
    assert entry.main(["--db", db_path, "crawl", "--until", "22:30"]) == 0   # 8.5 h: fine


def test_crawl_deadline_for_refuses_more_than_nine_hours():
    evening = dt.datetime(2026, 10, 14, 21, 30)
    assert crawl.deadline_for("05:15", evening) == dt.datetime(2026, 10, 15, 5, 15)
    with pytest.raises(crawl.DeadlineError, match="15.2 h away"):
        crawl.deadline_for("05:15", dt.datetime(2026, 10, 14, 14, 0))
    with pytest.raises(crawl.DeadlineError, match="not HH:MM"):
        crawl.deadline_for("5pm", evening)


@pytest.mark.parametrize("command", [["nightly", "--env", "dev"], ["crawl"], ["export"],
                                     ["upload", "--env", "dev"]])
def test_second_dictionary_run_exits_while_the_first_holds_the_lock(nightly_env, command, capsys):
    db_path, phases, set_upload = nightly_env
    set_upload(ok=True)

    with entry.single_run(db_path):
        code = entry.main(["--db", db_path, *command])

    assert code == 3 and phases == []
    assert "another dictionary run holds" in capsys.readouterr().err
    assert entry.main(["--db", db_path, *command]) == 0      # free again once released


# ---------------------------------------------------------------------------
# 409s on the commit
# ---------------------------------------------------------------------------

def test_upload_stale_version_naming_our_version_is_committed(tmp_path):
    out, _ = _export_dir(tmp_path, shards=1)
    api = _api()
    api.post_dictionary_commit.side_effect = PoolApiError(
        409, "stale_version", "active version is 2026-10-14T05:40Z")
    sleep = MagicMock()

    outcome = upload.upload(api, out, "dev", sleep=sleep)

    assert outcome.ok and outcome.committed
    assert api.post_dictionary_commit.call_count == 1
    sleep.assert_not_called()


def test_upload_stale_version_naming_another_version_fails(tmp_path):
    out, _ = _export_dir(tmp_path, shards=1)
    api = _api()
    api.post_dictionary_commit.side_effect = PoolApiError(
        409, "stale_version", "active version is 2026-10-15T05:40Z")

    outcome = upload.upload(api, out, "dev", sleep=MagicMock())

    assert not outcome.ok and not outcome.committed
    assert api.post_dictionary_commit.call_count == 1


@pytest.mark.parametrize("error", ["incomplete_version", "commit_conflict"])
def test_upload_retries_the_commit_once(tmp_path, error):
    out, built = _export_dir(tmp_path, shards=1)
    api = _api()
    api.post_dictionary_commit.side_effect = [PoolApiError(409, error, "x"), {"version": built.version}]
    sleep = MagicMock()

    outcome = upload.upload(api, out, "dev", sleep=sleep)

    assert outcome.ok and api.post_dictionary_commit.call_count == 2
    sleep.assert_called_once_with(upload.COMMIT_RETRY_PAUSE_S)
    assert api.post_dictionary_shard.call_count == 1          # shards are not re-posted

    api = _api()
    api.post_dictionary_commit.side_effect = PoolApiError(409, error, "x")
    outcome = upload.upload(api, out, "dev", sleep=MagicMock())
    assert not outcome.ok and api.post_dictionary_commit.call_count == 2
    assert outcome.error == f"status=409 error={error} detail=x"


# ---------------------------------------------------------------------------
# The taxonomy gate
# ---------------------------------------------------------------------------

def _settings_for_dev(monkeypatch):
    monkeypatch.setenv("TUNEFINDER_POOL_API_DEV", "https://dev.example")
    from src.config import load_settings

    return load_settings()


def test_upload_to_env_checks_the_taxonomy_before_any_shard(tmp_path, monkeypatch):
    out, _ = _export_dir(tmp_path, shards=2)
    settings = _settings_for_dev(monkeypatch)
    api = _api()
    api.get_config.return_value = {"taxonomy_version": load_taxonomy().version + 1}

    outcome = upload.upload_to_env(settings, "dev", out, client_factory=MagicMock(return_value=api))

    api.get_config.assert_called_once_with()
    api.post_dictionary_shard.assert_not_called()
    api.post_dictionary_commit.assert_not_called()
    assert not outcome.ok and "runs taxonomy v" in outcome.error


def test_upload_to_env_refuses_when_the_config_is_unreadable(tmp_path, monkeypatch):
    out, _ = _export_dir(tmp_path, shards=1)
    settings = _settings_for_dev(monkeypatch)
    api = _api()
    api.get_config.side_effect = PoolApiError(None, "transport", "ConnectionError")

    outcome = upload.upload_to_env(settings, "dev", out, client_factory=MagicMock(return_value=api))

    api.post_dictionary_shard.assert_not_called()
    assert not outcome.ok and "ingest config unreadable" in outcome.error


def test_upload_to_env_uploads_when_the_taxonomy_matches(tmp_path, monkeypatch):
    out, _ = _export_dir(tmp_path, shards=1)
    settings = _settings_for_dev(monkeypatch)
    api = _api()

    outcome = upload.upload_to_env(settings, "dev", out, client_factory=MagicMock(return_value=api))

    assert outcome.ok
    assert [c[0] for c in api.mock_calls] == ["get_config", "post_dictionary_shard", "post_dictionary_commit"]
