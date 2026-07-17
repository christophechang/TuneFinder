import json
import time
import urllib.parse
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.fetchers import soundcloud


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path, enabled=True, targets=None, downloadable_only=True,
                   lookback_days=28, limit=50, client_id="cid", client_secret="csec"):
    s = MagicMock()
    if targets is None:
        targets = [{"tf_tag": "dnb", "genres": "drum & bass"}]
    s.get_source_config.return_value = {
        "enabled": enabled,
        "downloadable_only": downloadable_only,
        "lookback_days": lookback_days,
        "limit_per_target": limit,
        "targets": targets,
    }
    s.data_dir = str(tmp_path)
    s.soundcloud_client_id = client_id
    s.soundcloud_client_secret = client_secret
    return s


def _track(track_id=123456789, title="Test Track (Bootleg)", username="Test DJ",
           downloadable=True, created_at="2026-07-10T07:00:00Z", label_name="Test Label"):
    return {
        "id": track_id,
        "urn": f"soundcloud:tracks:{track_id}",
        "title": title,
        "user": {"username": username},
        "permalink_url": f"https://soundcloud.com/test-dj/track-{track_id}",
        "created_at": created_at,
        "downloadable": downloadable,
        "download_count": 42,
        "playback_count": 1000,
        "favoritings_count": 50,
        "purchase_url": "https://hypeddit.com/dl/xyz",
        "purchase_title": "Free Download",
        "license": "all-rights-reserved",
        "genre": "Drum & Bass",
        "tag_list": "dnb \"free download\"",
        "duration": 240000,
        "label_name": label_name,
        "metadata_artist": None,
        "bpm": None,
        "key_signature": None,
        "reposts_count": 7,
        "release_year": None,
        "release_month": None,
        "release_day": None,
    }


def _page(tracks, next_href=None):
    return {"collection": tracks, "next_href": next_href}


def _patch_token():
    return patch("src.fetchers.soundcloud._get_access_token", return_value="tok")


# ---------------------------------------------------------------------------
# Disabled / unconfigured
# ---------------------------------------------------------------------------

def test_fetch_disabled_returns_empty(tmp_path):
    assert soundcloud.fetch(_make_settings(tmp_path, enabled=False)) == []


def test_fetch_no_targets_returns_empty(tmp_path):
    assert soundcloud.fetch(_make_settings(tmp_path, targets=[])) == []


def test_fetch_missing_credentials_raises(tmp_path):
    settings = _make_settings(tmp_path, client_id="", client_secret="")
    with pytest.raises(soundcloud.SoundCloudAuthError):
        soundcloud.fetch(settings)


# ---------------------------------------------------------------------------
# Basic fetch and parsing
# ---------------------------------------------------------------------------

def test_fetch_returns_source_items(tmp_path):
    settings = _make_settings(tmp_path)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([_track()])
        items = soundcloud.fetch(settings)

    assert len(items) == 1
    item = items[0]
    assert item.source == "soundcloud"
    assert item.artist == "Test DJ"
    assert item.title == "Test Track (Bootleg)"
    assert item.link == "https://soundcloud.com/test-dj/track-123456789"
    assert item.label == "Test Label"
    assert item.release_date == "2026-07-10"
    assert item.genre_tags == ["dnb"]


def test_fetch_raw_metadata_fields(tmp_path):
    settings = _make_settings(tmp_path)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([_track()])
        items = soundcloud.fetch(settings)

    md = items[0].raw_metadata
    assert md["soundcloud_id"] == 123456789
    assert md["downloadable"] is True
    assert md["download_count"] == 42
    assert md["playback_count"] == 1000
    assert md["purchase_url"] == "https://hypeddit.com/dl/xyz"
    assert md["license"] == "all-rights-reserved"
    assert md["sc_genre"] == "Drum & Bass"
    assert md["duration_ms"] == 240000


def test_release_date_normalises_legacy_format(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track(created_at="2026/07/10 07:00:00 +0000")
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)

    assert items[0].release_date == "2026-07-10"


def test_link_strips_tracking_query_params(tmp_path):
    """The API appends utm_* query params to permalink_url — links must be clean."""
    settings = _make_settings(tmp_path)
    track = _track()
    track["permalink_url"] = "https://soundcloud.com/dj/track-1?utm_medium=api&utm_source=id_123"
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)

    assert items[0].link == "https://soundcloud.com/dj/track-1"


def test_parse_track_missing_username_skipped(tmp_path):
    settings = _make_settings(tmp_path)
    bad = _track()
    bad["user"] = {}
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([bad, _track()])
        items = soundcloud.fetch(settings)

    assert len(items) == 1


def test_duration_guard_drops_live_sets(tmp_path):
    """SoundCloud mixes tracks and full DJ sets in search results — anything over
    max_duration_minutes is dropped (0 disables; missing duration passes)."""
    settings = _make_settings(tmp_path)
    track_ok = _track(track_id=1)                      # 4 min
    live_set = _track(track_id=2)
    live_set["duration"] = 65 * 60 * 1000              # 65 min
    no_duration = _track(track_id=3)
    no_duration["duration"] = None
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track_ok, live_set, no_duration])
        items = soundcloud.fetch(settings)

    assert [i.raw_metadata["soundcloud_id"] for i in items] == [1, 3]


def test_duration_guard_zero_disables(tmp_path):
    settings = _make_settings(tmp_path)
    settings.get_source_config.return_value["max_duration_minutes"] = 0
    live_set = _track(track_id=2)
    live_set["duration"] = 65 * 60 * 1000
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([live_set])
        items = soundcloud.fetch(settings)

    assert len(items) == 1


def test_lookback_window_enforced_client_side(tmp_path):
    """The API ignores created_at[from] (verified live 2026-07-17) — the fetcher
    must drop out-of-window tracks itself. Undated tracks pass (pipeline semantics)."""
    settings = _make_settings(tmp_path, lookback_days=28)
    recent = _track(track_id=1, created_at=f"{(date.today() - timedelta(days=3)).isoformat()}T07:00:00Z")
    stale = _track(track_id=2, created_at=f"{(date.today() - timedelta(days=90)).isoformat()}T07:00:00Z")
    undated = _track(track_id=3, created_at=None)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([recent, stale, undated])
        items = soundcloud.fetch(settings)

    ids = [i.raw_metadata["soundcloud_id"] for i in items]
    assert ids == [1, 3]


# ---------------------------------------------------------------------------
# Downloadable filter
# ---------------------------------------------------------------------------

def test_downloadable_only_filters_non_downloadable(tmp_path):
    settings = _make_settings(tmp_path, downloadable_only=True)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([_track(track_id=1), _track(track_id=2, downloadable=False)])
        items = soundcloud.fetch(settings)

    assert len(items) == 1
    assert items[0].raw_metadata["soundcloud_id"] == 1


def test_downloadable_only_false_keeps_all(tmp_path):
    settings = _make_settings(tmp_path, downloadable_only=False)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([_track(track_id=1), _track(track_id=2, downloadable=False)])
        items = soundcloud.fetch(settings)

    assert len(items) == 2


# ---------------------------------------------------------------------------
# Search parameters
# ---------------------------------------------------------------------------

def test_search_params_include_filters_and_window(tmp_path):
    settings = _make_settings(
        tmp_path,
        targets=[{"tf_tag": "ukg", "genres": "uk garage,garage", "tags": "ukg", "q": "free download"}],
        lookback_days=7,
        limit=25,
    )
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([])
        soundcloud.fetch(settings)

    url = mock_get.call_args[0][0]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["genres"] == ["uk garage,garage"]
    assert qs["tags"] == ["ukg"]
    assert qs["q"] == ["free download"]
    assert qs["limit"] == ["25"]
    assert qs["linked_partitioning"] == ["true"]
    expected_from = (date.today() - timedelta(days=7)).isoformat()
    assert qs["created_at[from]"] == [f"{expected_from} 00:00:00"]


def test_target_genre_narrows_targets(tmp_path):
    settings = _make_settings(
        tmp_path,
        targets=[
            {"tf_tag": "dnb", "genres": "drum & bass"},
            {"tf_tag": "house", "genres": "house"},
        ],
    )
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([])
        soundcloud.fetch(settings, target_genre="house")

    assert mock_get.call_count == 1
    url = mock_get.call_args[0][0]
    assert "house" in url
    assert "drum" not in url


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_pagination_follows_next_href(tmp_path):
    settings = _make_settings(tmp_path)
    next_url = "https://api.soundcloud.com/tracks?cursor=abc"
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.side_effect = [
            _page([_track(track_id=1)], next_href=next_url),
            _page([_track(track_id=2)]),
        ]
        items = soundcloud.fetch(settings)

    assert len(items) == 2
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[1][0][0] == next_url


def test_pagination_caps_at_max_pages(tmp_path):
    settings = _make_settings(tmp_path)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([_track()], next_href="https://api.soundcloud.com/tracks?cursor=x")
        soundcloud.fetch(settings)

    assert mock_get.call_count == soundcloud._MAX_PAGES


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_all_targets_failed_raises(tmp_path):
    settings = _make_settings(
        tmp_path,
        targets=[
            {"tf_tag": "dnb", "genres": "drum & bass"},
            {"tf_tag": "house", "genres": "house"},
        ],
    )
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get, \
            patch("src.fetchers.soundcloud.polite_sleep"):
        mock_get.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="all 2 targets failed"):
            soundcloud.fetch(settings)


def test_one_failed_target_continues(tmp_path):
    settings = _make_settings(
        tmp_path,
        targets=[
            {"tf_tag": "dnb", "genres": "drum & bass"},
            {"tf_tag": "house", "genres": "house"},
        ],
    )
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get, \
            patch("src.fetchers.soundcloud.polite_sleep"):
        mock_get.side_effect = [RuntimeError("boom"), _page([_track()])]
        items = soundcloud.fetch(settings)

    assert len(items) == 1


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------

def test_soundcloud_registered_in_fetcher_registry():
    from src.fetchers import _FETCHERS
    assert any(name == "soundcloud" and fn is soundcloud.fetch for name, fn in _FETCHERS)


# ---------------------------------------------------------------------------
# Token cache (client_credentials)
# ---------------------------------------------------------------------------

def test_token_uses_valid_cache(tmp_path):
    cache = {"access_token": "cached-tok", "expires_at": time.time() + 3600}
    (tmp_path / "soundcloud_token.json").write_text(json.dumps(cache))
    settings = _make_settings(tmp_path)

    with patch("src.fetchers.soundcloud._fetch_token") as mock_fetch:
        token = soundcloud._get_access_token(settings, MagicMock())

    assert token == "cached-tok"
    mock_fetch.assert_not_called()


def test_token_fetches_and_persists_when_no_cache(tmp_path):
    settings = _make_settings(tmp_path)

    with patch("src.fetchers.soundcloud._fetch_token") as mock_fetch:
        mock_fetch.return_value = {"access_token": "fresh-tok", "expires_in": 3599}
        token = soundcloud._get_access_token(settings, MagicMock())

    assert token == "fresh-tok"
    saved = json.loads((tmp_path / "soundcloud_token.json").read_text())
    assert saved["access_token"] == "fresh-tok"
    assert saved["expires_at"] > time.time()


def test_token_expired_cache_refetches(tmp_path):
    cache = {"access_token": "stale-tok", "expires_at": time.time() - 10}
    (tmp_path / "soundcloud_token.json").write_text(json.dumps(cache))
    settings = _make_settings(tmp_path)

    with patch("src.fetchers.soundcloud._fetch_token") as mock_fetch:
        mock_fetch.return_value = {"access_token": "fresh-tok", "expires_in": 3599}
        token = soundcloud._get_access_token(settings, MagicMock())

    assert token == "fresh-tok"


def test_token_missing_credentials_raises(tmp_path):
    settings = _make_settings(tmp_path, client_id="", client_secret="")
    with pytest.raises(soundcloud.SoundCloudAuthError):
        soundcloud._get_access_token(settings, MagicMock())


# ---------------------------------------------------------------------------
# Parse extras (artist, bpm, key, reposts, release stash)
# ---------------------------------------------------------------------------

def test_parse_extracts_bpm_key_reposts_and_release_fields(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track()
    track.update({"bpm": 174.0, "key_signature": "Am", "reposts_count": 33,
                  "release_year": 2005, "release_month": 6, "release_day": 1})
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)

    md = items[0].raw_metadata
    assert md["bpm"] == 174.0
    assert md["key"] == "Am"
    assert md["reposts_count"] == 33
    assert (md["release_year"], md["release_month"], md["release_day"]) == (2005, 6, 1)
    # release_* are display-only stash — the pipeline release date stays upload-derived
    assert items[0].release_date == "2026-07-10"


def test_parse_prefers_metadata_artist_over_username(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track()
    track["metadata_artist"] = "Real Artist"
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)
    assert items[0].artist == "Real Artist"


def test_parse_blank_metadata_artist_falls_back_to_username(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track()
    track["metadata_artist"] = "   "
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)
    assert items[0].artist == "Test DJ"
