"""Run services — src/services/runs.py.

The CLI-level behaviour (dry-run write gating, label affinity, degraded
profile mode) is covered in test_labels.py / test_degraded_profile.py via
cmd_run / cmd_mix_prep, which now delegate here. These tests cover what the
service layer adds: RunOutcome, the report artifact, progress events, and
run-lock contention.
"""
import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from src.models import SourceItem, Track
from src.pipeline.ranker import ScoringWeights
from src.pipeline.storage import RunLockHeldError, run_lock
from src.services.runs import (
    MIX_PREP_GENRES,
    MixPrepOptions,
    WeeklyRunOptions,
    run_mix_prep,
    run_weekly,
)


def _settings(data_dir):
    settings = MagicMock()
    settings.data_dir = data_dir
    settings.pipeline_release_date_window_days = None
    settings.pipeline_remix_aware_identity = False
    settings.alerts_source_drop_threshold_pct = 50
    settings.alerts_min_history_runs = 2
    settings.scoring_weights = MagicMock(return_value=ScoringWeights())
    settings.artist_aliases = MagicMock(return_value={})
    settings.pipeline_top_picks_count = 5
    settings.pipeline_label_watch_count = 5
    settings.pipeline_artist_watch_count = 5
    settings.pipeline_wildcard_count = 3
    settings.pipeline_section_min_score = 0.0
    settings.pipeline_mix_prep_top_picks_count = 20
    settings.pipeline_mix_prep_deep_cuts_count = 20
    settings.pipeline_free_download_sources = []
    settings.pipeline_free_downloads_count = 5
    settings.pipeline_mix_prep_free_downloads_count = 10
    settings.pipeline_free_downloads_min_score = 0.0
    settings.pipeline_genre_exclusions = {}
    settings.discord_mix_prep_channel = "mix-prep"
    settings.validate = MagicMock()
    return settings


def _source_item():
    return SourceItem(
        source="beatport", artist="Sully", title="New Track", link="https://example.com/x",
        label="Astrophonica", release_date=None, genre_tags=["breaks"],
        raw_metadata={"beatport_id": 42, "bpm": 140},
    )


def _known_track():
    return Track(artist="Sully", title="Old Track", recurrence_count=2, genres_seen=["breaks"])


def _patched(fn, settings, options, progress=None):
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources", return_value=([_source_item()], {"beatport": {"count": 1, "error": None}})), \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        return fn(settings, options, progress=progress)


def test_run_weekly_live_outcome_and_artifact(tmp_path):
    settings = _settings(str(tmp_path))
    outcome = _patched(run_weekly, settings, WeeklyRunOptions(dry_run=False))

    assert outcome.kind == "weekly"
    assert outcome.dry_run is False
    assert outcome.recommended_count == 1
    assert outcome.no_candidates is False
    assert outcome.report_text
    assert outcome.stats["sources_fetched"] == 1

    # Artifact persisted, loadable, and consistent with the outcome
    assert outcome.artifact_path is not None
    with open(outcome.artifact_path) as f:
        stored = json.load(f)
    assert stored == outcome.artifact
    assert stored["kind"] == "weekly"
    assert stored["report_id"] == outcome.report_id
    track = stored["sections"][0]["tracks"][0]
    assert track["artist"] == "Sully"
    assert track["embed"] == {"type": "beatport", "track_id": 42}
    # audition page written alongside
    assert outcome.audition_path is not None


def test_run_weekly_dry_run_builds_artifact_but_writes_nothing(tmp_path):
    settings = _settings(str(tmp_path))
    outcome = _patched(run_weekly, settings, WeeklyRunOptions(dry_run=True))

    assert outcome.dry_run is True
    assert outcome.artifact is not None
    assert outcome.artifact["dry_run"] is True
    assert outcome.artifact_path is None
    assert outcome.audition_path is None
    assert not (tmp_path / "reports").exists()
    assert not (tmp_path / "recommendation_history.json").exists()
    assert "DRY RUN" in outcome.report_text


def test_run_weekly_emits_progress_stages(tmp_path):
    settings = _settings(str(tmp_path))
    events = []
    _patched(run_weekly, settings, WeeklyRunOptions(dry_run=True),
             progress=lambda stage, detail: events.append(stage))

    stages = [s for i, s in enumerate(events) if s not in events[:i]]  # first occurrence order
    assert stages == ["profile", "sources", "filter", "rank", "report", "deliver", "done"]


def test_run_weekly_lock_contention_raises(tmp_path):
    settings = _settings(str(tmp_path))
    with run_lock(str(tmp_path)):
        with pytest.raises(RunLockHeldError):
            _patched(run_weekly, settings, WeeklyRunOptions(dry_run=True))


def test_run_weekly_no_candidates_outcome(tmp_path):
    settings = _settings(str(tmp_path))
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources", return_value=([], {})), \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        outcome = run_weekly(settings, WeeklyRunOptions(dry_run=True))

    assert outcome.no_candidates is True
    assert outcome.recommended_count == 0
    assert outcome.artifact is None


def test_run_mix_prep_live_outcome_and_artifact(tmp_path):
    settings = _settings(str(tmp_path))
    options = MixPrepOptions(genre="breaks", bpm_range=(130.0, 150.0), key_camelot=None,
                             bpm_flex=True, dry_run=False)
    outcome = _patched(run_mix_prep, settings, options)

    assert outcome.kind == "mix-prep"
    assert outcome.recommended_count == 1
    assert outcome.report_id.endswith("-mix-prep-breaks")
    assert outcome.artifact_path is not None
    with open(outcome.artifact_path) as f:
        stored = json.load(f)
    assert stored["genre"] == "breaks"
    assert stored["filters"]["bpm_min"] == 130.0
    assert stored["filters"]["bpm_max"] == 150.0
    assert stored["filters"]["key_camelot"] is None
    assert "BPM 130–150" in stored["filters"]["description"]


def test_run_mix_prep_dry_run_writes_nothing(tmp_path):
    settings = _settings(str(tmp_path))
    options = MixPrepOptions(genre="breaks", dry_run=True)
    outcome = _patched(run_mix_prep, settings, options)

    assert outcome.artifact is not None
    assert outcome.artifact_path is None
    assert not (tmp_path / "mix_prep_history.json").exists()
    assert not (tmp_path / "reports").exists()


def test_run_mix_prep_lock_contention_raises(tmp_path):
    settings = _settings(str(tmp_path))
    with run_lock(str(tmp_path)):
        with pytest.raises(RunLockHeldError):
            _patched(run_mix_prep, settings, MixPrepOptions(genre="breaks", dry_run=True))


def test_cli_run_reports_lock_contention_cleanly(tmp_path, capsys):
    from tunefinder.__main__ import cmd_run

    settings = _settings(str(tmp_path))
    with run_lock(str(tmp_path)):
        with patch("tunefinder.__main__.load_settings", return_value=settings):
            with pytest.raises(SystemExit) as excinfo:
                cmd_run(argparse.Namespace(dry_run=True))
    assert excinfo.value.code == 1
    assert "another TuneFinder run is in progress" in capsys.readouterr().out


def test_mix_prep_genres_match_cli_choices():
    assert "house" in MIX_PREP_GENRES and "dnb" in MIX_PREP_GENRES
    assert len(MIX_PREP_GENRES) == 10


def _free_item(title="Boot VIP", free=True):
    md = {"soundcloud_id": 1, "download_count": 60}
    if free:
        md.update({"free_download": True, "free_gate": False,
                   "acquisition_url": "https://soundcloud.com/x/y"})
    return SourceItem(
        source="soundcloud", artist="Someone", title=title, link="https://soundcloud.com/x/y",
        label=None, release_date=None, genre_tags=["dnb"], raw_metadata=md,
    )


def _seed_pool(data_dir):
    """A free SoundCloud pool record and a paid Beatport one — the free_only
    eligibility filter must let only the first into the report."""
    from datetime import datetime, timezone
    from src.models import PoolRecord
    from src.pipeline.pool import save_pool
    # added_at is "just now" rather than a fixed past date — the ranker's
    # pool-age penalty (src/pipeline/ranker.py, ~0.25/week) is real-clock-driven,
    # so a fixed date drifts further into penalty range every day this test
    # suite runs. This test is about the free_only eligibility filter, not
    # pool-age scoring, so keep the pool entries fresh.
    now_iso = datetime.now(timezone.utc).isoformat()
    save_pool([
        PoolRecord(artist="PoolFree", title="Pool Boot", link="https://soundcloud.com/p/f",
                   source="soundcloud", label=None, release_date=None, release_name=None,
                   genre_tags=["dnb"],
                   raw_metadata={"free_download": True, "free_gate": False,
                                 "acquisition_url": "https://soundcloud.com/p/f"},
                   added_at=now_iso, last_score=1.0),
        PoolRecord(artist="PoolPaid", title="Pool Paid", link="https://example.com/p",
                   source="beatport", label=None, release_date=None, release_name=None,
                   genre_tags=["dnb"], raw_metadata={"beatport_id": 7},
                   added_at=now_iso, last_score=1.0),
    ], data_dir)


def test_run_free_only_restricts_fetch_and_filters_eligibility(tmp_path):
    settings = _settings(str(tmp_path))
    settings.pipeline_free_download_sources = ["soundcloud"]
    settings.pipeline_free_downloads_mode_count = 30
    _seed_pool(str(tmp_path))
    options = MixPrepOptions(genre="dnb", dry_run=True, free_only=True)
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources",
               return_value=([_free_item(), _free_item(title="Paid Leak", free=False)],
                             {"soundcloud": {"count": 2, "error": None}})) as mock_fetch, \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        outcome = run_mix_prep(settings, options)

    assert mock_fetch.call_args.kwargs["only_sources"] == ["soundcloud"]
    assert outcome.kind == "free-downloads"
    assert "-free-dl-dnb" in outcome.report_id
    assert outcome.artifact["kind"] == "free-downloads"
    titles = [t["title"] for s in outcome.artifact["sections"] for t in s["tracks"]]
    # fresh: free kept, paid dropped; pool: free injected, paid filtered
    assert "Boot VIP" in titles and "Paid Leak" not in titles
    assert "Pool Boot" in titles and "Pool Paid" not in titles


def test_run_free_only_forwards_expanded_bpm_ranges(tmp_path):
    settings = _settings(str(tmp_path))
    settings.pipeline_free_download_sources = ["soundcloud"]
    settings.pipeline_free_downloads_mode_count = 30
    options = MixPrepOptions(genre="dnb", bpm_range=(170.0, 180.0), dry_run=True, free_only=True)
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources",
               return_value=([_free_item()], {"soundcloud": {"count": 1, "error": None}})) as mock_fetch, \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        run_mix_prep(settings, options)
    assert mock_fetch.call_args.kwargs["bpm_ranges"] == [(170.0, 180.0), (85.0, 90.0), (340.0, 360.0)]


def test_run_mix_prep_regular_sends_no_only_sources_or_bpm_ranges(tmp_path):
    settings = _settings(str(tmp_path))
    options = MixPrepOptions(genre="breaks", bpm_range=(130.0, 140.0), dry_run=True)
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources",
               return_value=([_source_item()], {"beatport": {"count": 1, "error": None}})) as mock_fetch, \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        run_mix_prep(settings, options)
    assert mock_fetch.call_args.kwargs.get("only_sources") is None
    assert mock_fetch.call_args.kwargs.get("bpm_ranges") is None
