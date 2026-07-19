"""Web API — src/web (app, auth, reportdata, jobs)."""
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.models import Candidate, RecommendationSignal, SourceItem, Track
from src.pipeline.report_artifact import build_report_artifact, write_report_artifact
from src.web.app import create_app
from src.web.auth import AuthConfigError

SECRET = "test-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


# ---------------------------------------------------------------------------
# Fixtures / seed helpers
# ---------------------------------------------------------------------------

def _settings(tmp_path) -> Settings:
    return Settings({"data_dir": str(tmp_path)})


def _seed_history(tmp_path):
    weekly = [
        # Superseded batch: 2026-W27 was run once, then re-run. History is
        # append-only so both batches live under the one report_id, reusing
        # track numbers — the shape every consumer of a track number has to
        # cope with. Seeded by default so tests inherit the realistic store
        # rather than a one-run-per-report fiction.
        {"artist": "Superseded", "title": "Dropped", "link": "https://example.com/z", "source": "beatport",
         "recommended_at": "2026-07-03T09:00:00+00:00", "report_id": "2026-W27", "track_no": 1,
         "signal_codes": [], "genre_tags": ["breaks"], "score": 4.0, "label": None},
        {"artist": "Sully", "title": "Alpha", "link": "https://example.com/a", "source": "beatport",
         "recommended_at": "2026-07-05T09:00:00+00:00", "report_id": "2026-W27", "track_no": 1,
         "signal_codes": ["known_artist"], "genre_tags": ["breaks"], "score": 7.5, "label": "Astrophonica"},
        {"artist": "Skee Mask", "title": "Beta", "link": "https://example.com/b", "source": "bandcamp",
         "recommended_at": "2026-07-05T09:00:00+00:00", "report_id": "2026-W27", "track_no": 2,
         "signal_codes": ["genre_match"], "genre_tags": ["electronica"], "score": 3.0, "label": None},
        {"artist": "Old Artist", "title": "Old Track", "link": "https://example.com/o", "source": "volumo",
         "recommended_at": "2026-06-28T09:00:00+00:00", "report_id": "2026-W26", "track_no": 1,
         "signal_codes": [], "genre_tags": ["house"], "score": 2.0, "label": None},
    ]
    (tmp_path / "recommendation_history.json").write_text(json.dumps(weekly))
    mix_prep = [
        {"artist": "Calibre", "title": "Gamma", "link": "https://example.com/c", "source": "volumo",
         "recommended_at": "2026-07-06T10:00:00+00:00", "report_id": "2026-W27-mix-prep-dnb", "track_no": 1,
         "signal_codes": ["known_artist"], "genre_tags": ["dnb"], "score": 9.0, "label": "Signature"},
    ]
    (tmp_path / "mix_prep_history.json").write_text(json.dumps(mix_prep))


def _seed_artifact(tmp_path):
    c = Candidate(
        artist="Sully", title="Alpha", link="https://example.com/a", source="beatport",
        label="Astrophonica", genre_tags=["breaks"],
        raw_metadata={"beatport_id": 42, "bpm": 140, "seen_on_sources": ["beatport"]},
    )
    c.score, c.familiarity_score, c.discovery_score = 7.5, 6.0, 1.5
    c.signals = [RecommendationSignal("known_artist", "You play Sully.")]
    artifact = build_report_artifact(
        {"top_picks": [c]}, "2026-W27", "weekly", {"sources_fetched": 100},
        generated_at="2026-07-05T09:00:00+00:00",
    )
    write_report_artifact(artifact, str(tmp_path))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNEFINDER_API_SECRET", SECRET)
    monkeypatch.delenv("TUNEFINDER_WEB_INSECURE", raising=False)
    monkeypatch.delenv("TUNEFINDER_WEB_STATIC_DIR", raising=False)
    _seed_history(tmp_path)
    _seed_artifact(tmp_path)
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        c.tmp_path = tmp_path
        yield c


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_missing_secret_refuses_to_start(tmp_path, monkeypatch):
    monkeypatch.delenv("TUNEFINDER_API_SECRET", raising=False)
    monkeypatch.delenv("TUNEFINDER_WEB_INSECURE", raising=False)
    with pytest.raises(AuthConfigError):
        create_app(_settings(tmp_path))


def test_insecure_opt_out_allows_start_and_open_access(tmp_path, monkeypatch):
    monkeypatch.delenv("TUNEFINDER_API_SECRET", raising=False)
    monkeypatch.setenv("TUNEFINDER_WEB_INSECURE", "1")
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/api/reports").status_code == 200


def test_requests_without_token_are_rejected(client):
    assert client.get("/api/reports").status_code == 401
    assert client.get("/api/reports", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_is_open_and_reports_latest(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_required"] is True
    assert body["latest_report_id"] == "2026-W27-mix-prep-dnb"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_reports_list_orders_and_flags(client):
    r = client.get("/api/reports", headers=AUTH)
    assert r.status_code == 200
    reports = r.json()["reports"]
    assert [x["report_id"] for x in reports] == ["2026-W27-mix-prep-dnb", "2026-W27", "2026-W26"]
    weekly = next(x for x in reports if x["report_id"] == "2026-W27")
    assert weekly["kind"] == "weekly" and weekly["track_count"] == 2 and weekly["has_artifact"] is True
    mp = reports[0]
    assert mp["kind"] == "mix-prep" and mp["genre"] == "dnb" and mp["has_artifact"] is False


def test_reports_list_counts_ignore_superseded_batches(client):
    """A re-run appends a second batch under one report_id. The summary counts
    tracks in the report, not history rows, so they must not double.
    """
    tmp_path = client.tmp_path
    weekly = json.loads((tmp_path / "recommendation_history.json").read_text())
    for at in ("2026-07-14T09:00:00+00:00", "2026-07-16T09:00:00+00:00"):
        weekly.append(
            {"artist": "Rerun Artist", "title": "Rerun Track", "link": "", "source": "beatport",
             "recommended_at": at, "report_id": "2026-W31", "track_no": 1,
             "signal_codes": [], "genre_tags": [], "score": 1.0, "label": None},
        )
    (tmp_path / "recommendation_history.json").write_text(json.dumps(weekly))

    client.post("/api/feedback", headers=AUTH,
                json={"outcome": "liked", "report_id": "2026-W31", "track_no": 1})

    summary = next(x for x in client.get("/api/reports", headers=AUTH).json()["reports"]
                   if x["report_id"] == "2026-W31")
    assert summary["track_count"] == 1
    assert summary["marked_count"] == 1


def test_reports_list_kind_filter(client):
    r = client.get("/api/reports?kind=mix-prep", headers=AUTH)
    assert [x["report_id"] for x in r.json()["reports"]] == ["2026-W27-mix-prep-dnb"]


def test_report_detail_artifact_backed(client):
    r = client.get("/api/reports/2026-W27", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is False
    track = body["sections"][0]["tracks"][0]
    assert track["artist"] == "Sully"
    assert track["reason"]
    assert track["embed"] == {"type": "beatport", "track_id": 42, "album_id": None, "url": None}
    assert track["signal_codes"] == ["known_artist"]
    assert track["feedback"] is None


def test_report_detail_degraded_from_history(client):
    r = client.get("/api/reports/2026-W26", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["sections"][0]["key"] == "tracks"
    assert body["sections"][0]["tracks"][0]["artist"] == "Old Artist"


def test_report_detail_degraded_collapses_rerun_batches(client):
    """No artifact + a re-run week: history holds two batches under one
    report_id reusing track numbers. The degraded view must show the newest run
    once, not both batches interleaved.
    """
    tmp_path = client.tmp_path
    weekly = json.loads((tmp_path / "recommendation_history.json").read_text())
    for artist, title, at in (
        ("Stale One", "Stale A", "2026-07-14T09:00:00+00:00"),
        ("Fresh One", "Fresh A", "2026-07-16T09:00:00+00:00"),
    ):
        weekly.append(
            {"artist": artist, "title": title, "link": "https://example.com/x", "source": "beatport",
             "recommended_at": at, "report_id": "2026-W30", "track_no": 1,
             "signal_codes": [], "genre_tags": [], "score": 1.0, "label": None},
        )
    (tmp_path / "recommendation_history.json").write_text(json.dumps(weekly))

    detail = client.get("/api/reports/2026-W30", headers=AUTH).json()
    assert detail["degraded"] is True
    tracks = detail["sections"][0]["tracks"]
    assert [t["artist"] for t in tracks] == ["Fresh One"]
    assert detail["track_count"] == 1


def test_report_detail_unknown_404(client):
    assert client.get("/api/reports/2020-W01", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def test_feedback_by_report_and_track_no(client):
    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "bought", "report_id": "2026-W27", "track_no": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["artist"] == "Sully" and body["history"] == "weekly"
    assert body["previous_outcome"] is None

    # joined into the report detail
    detail = client.get("/api/reports/2026-W27", headers=AUTH).json()
    assert detail["sections"][0]["tracks"][0]["feedback"]["outcome"] == "bought"

    # re-mark reports the previous outcome
    r2 = client.post("/api/feedback", headers=AUTH,
                     json={"outcome": "skip", "report_id": "2026-W27", "track_no": 1})
    assert r2.json()["previous_outcome"] == "bought"


def test_feedback_after_rerun_resolves_latest_batch(client):
    """A re-run appends a second batch under the same report_id; the artifact
    the SPA renders is the newer one. Marking track #1 must resolve against the
    newer batch, else the mark lands on the superseded track and read-back on
    the report detail shows nothing.
    """
    tmp_path = client.tmp_path
    weekly = json.loads((tmp_path / "recommendation_history.json").read_text())
    # Stale batch first in file order (as append-only history stores it), then
    # the re-run that overwrote the artifact.
    weekly.append(
        {"artist": "Stale Artist", "title": "Stale Track", "link": "https://example.com/s",
         "source": "beatport", "recommended_at": "2026-07-14T09:00:00+00:00",
         "report_id": "2026-W29", "track_no": 1, "signal_codes": [], "genre_tags": [], "score": 1.0,
         "label": None},
    )
    weekly.append(
        {"artist": "Fresh Artist", "title": "Fresh Track", "link": "https://example.com/f",
         "source": "beatport", "recommended_at": "2026-07-16T09:00:00+00:00",
         "report_id": "2026-W29", "track_no": 1, "signal_codes": [], "genre_tags": [], "score": 8.0,
         "label": None},
    )
    (tmp_path / "recommendation_history.json").write_text(json.dumps(weekly))

    fresh = Candidate(artist="Fresh Artist", title="Fresh Track", link="https://example.com/f",
                      source="beatport", label=None, genre_tags=[], raw_metadata={})
    fresh.score = 8.0
    write_report_artifact(
        build_report_artifact({"top_picks": [fresh]}, "2026-W29", "weekly", {},
                              generated_at="2026-07-16T09:00:00+00:00"),
        str(tmp_path),
    )

    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "heard", "report_id": "2026-W29", "track_no": 1})
    assert r.status_code == 200
    assert r.json()["artist"] == "Fresh Artist"

    detail = client.get("/api/reports/2026-W29", headers=AUTH).json()
    track = detail["sections"][0]["tracks"][0]
    assert track["artist"] == "Fresh Artist"
    assert track["feedback"] is not None, "mark did not survive read-back"
    assert track["feedback"]["outcome"] == "heard"


def test_feedback_mix_prep_report(client):
    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "liked", "report_id": "2026-W27-mix-prep-dnb", "track_no": 1})
    assert r.status_code == 200
    assert r.json()["history"] == "mix-prep"


def test_feedback_by_selector(client):
    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "own", "selector": "Skee Mask - Beta"})
    assert r.status_code == 200
    assert r.json()["artist"] == "Skee Mask"


def test_feedback_not_found_404(client):
    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "bought", "report_id": "2026-W27", "track_no": 99})
    assert r.status_code == 404


def test_feedback_heard_outcome_accepted(client):
    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "heard", "report_id": "2026-W27", "track_no": 1})
    assert r.status_code == 200
    assert r.json()["outcome"] == "heard"


def test_feedback_invalid_outcome_422(client):
    r = client.post("/api/feedback", headers=AUTH,
                    json={"outcome": "meh", "report_id": "2026-W27", "track_no": 1})
    assert r.status_code == 422


def test_feedback_stats_shape(client):
    client.post("/api/feedback", headers=AUTH,
                json={"outcome": "bought", "report_id": "2026-W27", "track_no": 1})
    r = client.get("/api/feedback/stats", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["weekly"]["marked"] == 1
    assert body["tune"]["marked"] == 1
    assert body["tune"]["thin_data"] is True
    assert "known_artist" in body["tune"]["dimensions"]["signal"]


# ---------------------------------------------------------------------------
# Explain / profile / pool / sources / config
# ---------------------------------------------------------------------------

def test_explain_returns_trace_text(client):
    with patch("src.pipeline.explain.explain_track", return_value="=== FETCHED ===\nok"):
        r = client.get("/api/explain", params={"selector": "Sully - Alpha"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["text"].startswith("=== FETCHED ===")


def test_profile_endpoint(client):
    (client.tmp_path / "artist_profiles.json").write_text(json.dumps({
        "Sully": {"name": "Sully", "play_count": 4, "genres_seen": ["breaks"],
                  "track_titles": ["Swandive"], "recency_weighted_play_count": 2.5},
    }))
    (client.tmp_path / "genre_affinity.json").write_text(json.dumps({"breaks": 1.0}))
    (client.tmp_path / "known_tracks.json").write_text(json.dumps(["sully||swandive"]))
    (client.tmp_path / "label_affinity.json").write_text(json.dumps({
        "astrophonica": {"display_name": "Astrophonica",
                         "artists": {"sully": {"name": "Sully", "last_seen": "2026-07-01T00:00:00+00:00"}},
                         "first_seen": "2026-01-01T00:00:00+00:00", "last_seen": "2026-07-01T00:00:00+00:00"},
    }))
    r = client.get("/api/profile", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["artist_count"] == 1 and body["known_track_count"] == 1
    assert body["top_artists"][0]["name"] == "Sully"
    assert body["genre_affinity"] == {"breaks": 1.0}
    assert body["labels"][0]["display_name"] == "Astrophonica"


def test_pool_endpoint(client):
    (client.tmp_path / "candidate_pool.json").write_text(json.dumps([
        {"artist": "A", "title": "T", "link": "", "source": "volumo",
         "added_at": "2026-07-01T00:00:00+00:00", "last_score": 4.0,
         "label": None, "release_date": None, "release_name": None,
         "genre_tags": ["house"], "raw_metadata": {}},
    ]))
    r = client.get("/api/pool", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["tracks"][0]["artist"] == "A"


def test_sources_health_endpoint(client):
    (client.tmp_path / "source_health.json").write_text(json.dumps([
        {"report_id": "2026-W27", "run_at": "2026-07-05T09:00:00+00:00",
         "health": {"beatport": {"count": 100, "error": None}}},
    ]))
    r = client.get("/api/sources/health", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["runs"][0]["report_id"] == "2026-W27"


def test_config_endpoint_sanitised(client):
    r = client.get("/api/config", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert set(body["sources"]) >= {"beatport", "volumo"}
    assert body["scoring"]["w_known_artist"] == 3.0
    assert body["genres"][0] == "dnb"
    assert "DISCORD" not in json.dumps(body).upper()


# ---------------------------------------------------------------------------
# Runs (jobs)
# ---------------------------------------------------------------------------

def _job_patches():
    return (
        patch("src.fetchers.catalog.fetch_all_tracks",
              return_value=[Track(artist="Sully", title="Old Track", recurrence_count=2, genres_seen=["breaks"])]),
        patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]),
        patch("src.fetchers.fetch_all_sources",
              return_value=([SourceItem(source="beatport", artist="Sully", title="Fresh",
                                        link="https://example.com/f", label="Astrophonica",
                                        genre_tags=["breaks"])], {"beatport": {"count": 1, "error": None}})),
        patch("src.output.discord.make_discord_client", return_value=MagicMock()),
    )


def _wait_for_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{job_id}", headers=AUTH).json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_run_weekly_dry_job_lifecycle(client):
    p1, p2, p3, p4 = _job_patches()
    with p1, p2, p3, p4:
        r = client.post("/api/runs", headers=AUTH, json={"mode": "weekly", "dry_run": True})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        body = _wait_for_job(client, job_id)

    assert body["status"] == "succeeded"
    assert body["dry_run"] is True
    assert body["recommended_count"] == 1
    assert body["report_id"]
    assert [s["stage"] for s in body["stages"]][0] == "profile"
    assert body["artifact"] is not None  # dry-run preview payload
    assert any("Starting report run" in line for line in body["log_tail"])

    # listed in /api/runs
    jobs = client.get("/api/runs", headers=AUTH).json()["jobs"]
    assert jobs[0]["id"] == job_id

    # persisted for restart survival
    persisted = json.loads((client.tmp_path / "web_jobs.json").read_text())
    assert persisted[0]["id"] == job_id


def test_run_mix_prep_live_job_writes_artifact(client):
    p1, p2, p3, p4 = _job_patches()
    with p1, p2, p3, p4:
        r = client.post("/api/runs", headers=AUTH,
                        json={"mode": "mix-prep", "genre": "breaks", "bpm_min": 130,
                              "bpm_max": 150, "dry_run": False})
        assert r.status_code == 202
        body = _wait_for_job(client, r.json()["job_id"])

    assert body["status"] == "succeeded"
    assert body["artifact"] is None  # live runs serve via /api/reports
    detail = client.get(f"/api/reports/{body['report_id']}", headers=AUTH).json()
    assert detail["kind"] == "mix-prep"
    assert detail["filters"]["bpm_min"] == 130.0


def test_run_conflict_409_while_running(client):
    release = threading.Event()
    started = threading.Event()

    def blocking_fetch(settings, target_genre=None):
        started.set()
        release.wait(timeout=15)
        return [], {}

    p1, p2, _, p4 = _job_patches()
    with p1, p2, p4, patch("src.fetchers.fetch_all_sources", side_effect=blocking_fetch):
        first = client.post("/api/runs", headers=AUTH, json={"mode": "weekly", "dry_run": True})
        assert first.status_code == 202
        assert started.wait(timeout=10)
        second = client.post("/api/runs", headers=AUTH, json={"mode": "weekly", "dry_run": True})
        assert second.status_code == 409
        release.set()
        _wait_for_job(client, first.json()["job_id"])


def test_run_validation_errors(client):
    assert client.post("/api/runs", headers=AUTH,
                       json={"mode": "mix-prep", "genre": "polka"}).status_code == 422
    assert client.post("/api/runs", headers=AUTH,
                       json={"mode": "mix-prep", "genre": "dnb", "bpm_min": 170}).status_code == 422
    assert client.post("/api/runs", headers=AUTH,
                       json={"mode": "mix-prep", "genre": "dnb", "key": "not-a-key"}).status_code == 422
    assert client.post("/api/runs", headers=AUTH,
                       json={"mode": "nonsense"}).status_code == 422
    assert client.get("/api/runs/unknown-id", headers=AUTH).status_code == 404


def test_jobs_survive_restart_as_history(client):
    p1, p2, p3, p4 = _job_patches()
    with p1, p2, p3, p4:
        r = client.post("/api/runs", headers=AUTH, json={"mode": "weekly", "dry_run": True})
        _wait_for_job(client, r.json()["job_id"])

    from src.web.jobs import JobManager
    fresh = JobManager(Settings({"data_dir": str(client.tmp_path)}))
    jobs = fresh.list()
    assert jobs and jobs[0].status == "succeeded"


def test_interrupted_job_marked_failed_on_restart(tmp_path, monkeypatch):
    (tmp_path / "web_jobs.json").write_text(json.dumps([
        {"id": "weekly-dead", "mode": "weekly", "status": "running", "dry_run": False,
         "params": {}, "created_at": "2026-07-10T00:00:00+00:00"},
    ]))
    from src.web.jobs import JobManager
    manager = JobManager(Settings({"data_dir": str(tmp_path)}))
    job = manager.get("weekly-dead")
    assert job.status == "failed"
    assert "interrupted" in job.error


# ---------------------------------------------------------------------------
# Free-downloads mode
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_free_dl_report(client):
    from src.models import RecommendationRecord
    from src.pipeline.history import append_mix_prep_records

    record = RecommendationRecord(
        artist="Rider Shafique", title="Free Cut", link="https://example.com/free",
        source="soundcloud", recommended_at="2026-07-17T09:00:00+00:00",
        report_id="2026-W29-free-dl-dnb", track_no=1,
        signal_codes=["known_artist"], genre_tags=["dnb"], score=5.0, label=None,
    )
    append_mix_prep_records([record], str(client.tmp_path))


def test_report_kind_free_dl_derivation():
    from src.web.reportdata import report_kind
    assert report_kind("2026-W29-free-dl-dnb") == ("free-downloads", "dnb")
    assert report_kind("2026-W29-mix-prep-dnb") == ("mix-prep", "dnb")
    assert report_kind("2026-W29") == ("weekly", None)


def test_build_options_free_downloads_mode():
    from src.web.jobs import build_options
    mode, options = build_options({"mode": "free-downloads", "genre": "dnb",
                                   "bpm_min": 170, "bpm_max": 180})
    assert mode == "free-downloads"
    assert options.free_only is True
    assert options.genre == "dnb" and options.bpm_range == (170.0, 180.0)


def test_build_options_free_downloads_requires_valid_genre():
    from src.web.jobs import build_options, JobValidationError
    with pytest.raises(JobValidationError):
        build_options({"mode": "free-downloads", "genre": "polka"})


def test_reports_list_accepts_free_downloads_kind(client):
    # the client fixture enables bearer auth — every request needs the
    # module's AUTH headers or it 401s before reaching the handler
    resp = client.get("/api/reports?kind=free-downloads", headers=AUTH)
    assert resp.status_code == 200


def test_feedback_on_free_dl_report_resolves_mix_prep_history(client, seeded_free_dl_report):
    resp = client.post("/api/feedback", headers=AUTH,
                       json={"outcome": "liked",
                             "report_id": "2026-W29-free-dl-dnb",
                             "track_no": 1})
    assert resp.status_code == 200
    assert resp.json()["history"] == "mix-prep"


# ---------------------------------------------------------------------------
# Static SPA mount
# ---------------------------------------------------------------------------

def test_static_spa_mount_serves_index_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNEFINDER_API_SECRET", SECRET)
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<html>tunefinder-web</html>")
    (static / "app.js").write_text("console.log(1)")
    monkeypatch.setenv("TUNEFINDER_WEB_STATIC_DIR", str(static))

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        assert "tunefinder-web" in c.get("/").text
        assert "tunefinder-web" in c.get("/reports/2026-W27").text  # SPA fallback
        assert c.get("/app.js").text.startswith("console.log")
        assert c.get("/api/health").json()["status"] == "ok"  # API still wins
