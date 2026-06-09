import urllib.parse
from unittest.mock import MagicMock, patch

from src.fetchers import volumo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(enabled=True, curation="curated", lookback_days=28, limit=50, genres=None):
    s = MagicMock()
    if genres is None:
        genres = [
            {"name": "house", "id": 12},
            {"name": "house", "id": 4},
            {"name": "dnb", "id": 6},
        ]
    s.get_source_config.return_value = {
        "enabled": enabled,
        "sort": "purchase",
        "curation": curation,
        "lookback_days": lookback_days,
        "limit_per_genre": limit,
        "genres": genres,
    }
    return s


def _album(album_id=100, title="Test EP", catalog="TEST001", tracks=None):
    return {
        "id": album_id,
        "title": title,
        "catalog_number": catalog,
        "release_start_at": "2026-05-01T00:00:00Z",
        "recordlabel": {"id": 9, "name": "Test Label"},
        "tracks": tracks or [_track()],
    }


def _track(track_id=6335989, title="Test Track", version=None, bpm=128, slug=None):
    return {
        "id": track_id,
        "title": title,
        "version": version,
        "slug": slug,
        "artists": [{"id": 1, "name": "Test Artist"}],
        "bpm": bpm,
        "keysign": "C major",
        "duration": 360000.0,
        "isrc": "GB1234567890",
        "release_start_at": "2026-05-01T00:00:00Z",
        "recordlabel": {"id": 9, "name": "Test Label"},
    }


# ---------------------------------------------------------------------------
# Disabled source
# ---------------------------------------------------------------------------

def test_fetch_disabled_returns_empty():
    assert volumo.fetch(_make_settings(enabled=False)) == []


# ---------------------------------------------------------------------------
# Basic fetch
# ---------------------------------------------------------------------------

def test_fetch_returns_source_items():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album()]
        items = volumo.fetch(settings)

    assert len(items) == 1
    item = items[0]
    assert item.source == "volumo"
    assert item.artist == "Test Artist"
    assert item.title == "Test Track"
    assert item.label == "Test Label"
    assert item.release_date == "2026-05-01"
    assert item.release_name == "Test EP"
    assert "house" in item.genre_tags


def test_fetch_raw_metadata_fields():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album()]
        items = volumo.fetch(settings)

    md = items[0].raw_metadata
    assert md["volumo_track_id"] == 6335989
    assert md["volumo_album_id"] == 100
    assert md["bpm"] == 128
    assert md["keysign"] == "C major"
    assert md["isrc"] == "GB1234567890"
    assert md["catalog_number"] == "TEST001"
    assert md["duration_ms"] == 360000.0
    assert md["label_name"] == "Test Label"
    assert md["label_id"] == 9


# ---------------------------------------------------------------------------
# Track link construction
# ---------------------------------------------------------------------------

def test_track_link_uses_api_slug_when_present():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    track = _track(slug="test-track-original-mix")
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[track])]
        items = volumo.fetch(settings)

    assert items[0].link == "https://volumo.com/track/6335989-test-track-original-mix"


def test_track_link_constructed_without_api_slug():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    track = _track(title="My Track", version="Original Mix", slug=None)
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[track])]
        items = volumo.fetch(settings)

    assert items[0].link == "https://volumo.com/track/6335989-my-track-original-mix"


def test_track_link_no_version():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    track = _track(title="My Track", version=None, slug=None)
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[track])]
        items = volumo.fetch(settings)

    assert items[0].link == "https://volumo.com/track/6335989-my-track"


# ---------------------------------------------------------------------------
# Genre grouping — multiple IDs batched per tag
# ---------------------------------------------------------------------------

def test_genre_ids_batched_per_tag():
    """house (id 12) and house (id 4) should produce ONE request, not two."""
    settings = _make_settings(genres=[{"name": "house", "id": 12}, {"name": "house", "id": 4}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = []
        volumo.fetch(settings)

    assert mock_get.call_count == 1
    url = mock_get.call_args[0][0]
    assert "12" in url and "4" in url


def test_duplicate_genre_ids_deduplicated():
    settings = _make_settings(genres=[
        {"name": "house", "id": 12},
        {"name": "house", "id": 12},
    ])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = []
        volumo.fetch(settings)

    assert mock_get.call_count == 1
    url = mock_get.call_args[0][0]
    import json as _json
    filter_str = urllib.parse.unquote(urllib.parse.urlparse(url).query.split("filter=")[1].split("&")[0])
    assert _json.loads(filter_str)["genres"] == [12]


# ---------------------------------------------------------------------------
# target_genre filter
# ---------------------------------------------------------------------------

def test_target_genre_filters_to_matching_tag():
    settings = _make_settings()
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = []
        volumo.fetch(settings, target_genre="dnb")

    assert mock_get.call_count == 1
    url = mock_get.call_args[0][0]
    assert "6" in url  # dnb id


def test_target_genre_no_match_returns_empty():
    settings = _make_settings()
    with patch("src.fetchers.volumo._get_json") as mock_get:
        items = volumo.fetch(settings, target_genre="funk-soul-jazz")

    assert items == []
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Curation filter
# ---------------------------------------------------------------------------

def test_curation_present_in_filter():
    settings = _make_settings(curation="curated", genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = []
        volumo.fetch(settings)

    url = mock_get.call_args[0][0]
    assert "curated" in url


def test_curation_absent_omitted_from_filter():
    settings = _make_settings(curation=None, genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = []
        volumo.fetch(settings)

    url = mock_get.call_args[0][0]
    assert "curation" not in url


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_paginates_on_full_page():
    """Full page (50 items) should trigger a second fetch."""
    settings = _make_settings(limit=2, genres=[{"name": "house", "id": 12}])
    full_page = [_album(album_id=i) for i in range(2)]
    empty_page = []
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.side_effect = [full_page, empty_page]
        volumo.fetch(settings)

    assert mock_get.call_count == 2


def test_stops_on_partial_page():
    """Partial page should stop pagination."""
    settings = _make_settings(limit=50, genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album()]  # 1 < 50 → stop
        volumo.fetch(settings)

    assert mock_get.call_count == 1


def test_max_3_pages_cap():
    settings = _make_settings(limit=1, genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album()]  # always full (1 item, limit=1)
        volumo.fetch(settings)

    assert mock_get.call_count == 3


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_http_error_returns_partial_results():
    """Error on second page still returns first page's results."""
    settings = _make_settings(limit=1, genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.side_effect = [
            [_album()],        # page 1 OK
            Exception("timeout"),  # page 2 fails
        ]
        items = volumo.fetch(settings)

    assert len(items) == 1


def test_http_error_on_first_page_returns_empty():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.side_effect = Exception("connection refused")
        items = volumo.fetch(settings)

    assert items == []


# ---------------------------------------------------------------------------
# Date validation
# ---------------------------------------------------------------------------

def test_is_valid_date_accepts_recent_year():
    assert volumo._is_valid_date("2026-05-01T00:00:00Z") is True
    assert volumo._is_valid_date("2020-01-01") is True


def test_is_valid_date_rejects_corrupted_year():
    assert volumo._is_valid_date("0009-01-29") is False
    assert volumo._is_valid_date("2099-01-01") is False


def test_is_valid_date_rejects_none_or_empty():
    assert volumo._is_valid_date(None) is False
    assert volumo._is_valid_date("") is False


def test_track_skipped_when_all_dates_invalid():
    """Track with only corrupted dates should be omitted."""
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    bad_track = {**_track(), "release_start_at": "0009-01-29"}
    album = {**_album(), "release_start_at": "0009-01-29", "first_live": None, "tracks": [bad_track]}
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [album]
        items = volumo.fetch(settings)

    assert items == []


def test_date_falls_back_to_first_live():
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    bad_track = {**_track(), "release_start_at": "0009-01-29"}
    album = {
        **_album(),
        "release_start_at": "0009-01-29",
        "first_live": "2026-04-15T12:00:00Z",
        "tracks": [bad_track],
    }
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [album]
        items = volumo.fetch(settings)

    assert len(items) == 1
    assert items[0].release_date == "2026-04-15"


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert volumo._slugify("My Track") == "my-track"
    assert volumo._slugify("Original Mix") == "original-mix"
    assert volumo._slugify("A♭ minor") == "a-minor"


def test_slugify_collapses_doubles():
    assert volumo._slugify("A  B") == "a-b"
    assert volumo._slugify("A--B") == "a-b"


# ---------------------------------------------------------------------------
# Track-level genre_id filtering (compilation album guard)
# ---------------------------------------------------------------------------

def test_track_with_matching_genre_id_is_included():
    """Track whose genre_id matches the queried genre passes through."""
    settings = _make_settings(genres=[{"name": "uk-bass", "id": 2}])
    track = {**_track(), "genre_id": 2}
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[track])]
        items = volumo.fetch(settings)

    assert len(items) == 1
    assert items[0].genre_tags == ["uk-bass"]


def test_track_with_mismatched_genre_id_is_excluded():
    """Compilation album: track genre_id 21 (tech house) in a genre-2 query is dropped."""
    settings = _make_settings(genres=[{"name": "uk-bass", "id": 2}])
    tech_house_track = {**_track(track_id=6234149, title="Perfect Love"), "genre_id": 21}
    album = _album(tracks=[tech_house_track])
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [album]
        items = volumo.fetch(settings)

    assert items == []


def test_track_without_genre_id_is_included():
    """Track with no genre_id field (API omits it) should not be filtered out."""
    settings = _make_settings(genres=[{"name": "house", "id": 12}])
    track = {k: v for k, v in _track().items() if k != "genre_id"}  # ensure absent
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[track])]
        items = volumo.fetch(settings)

    assert len(items) == 1


def test_genre_id_stored_in_raw_metadata():
    settings = _make_settings(genres=[{"name": "uk-bass", "id": 2}])
    track = {**_track(), "genre_id": 2}
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[track])]
        items = volumo.fetch(settings)

    assert items[0].raw_metadata["volumo_genre_id"] == 2


def test_compilation_album_partial_match():
    """Compilation with two tracks: one matching genre, one not — only matching survives."""
    settings = _make_settings(genres=[{"name": "uk-bass", "id": 2}])
    bass_track = {**_track(track_id=1, title="Bass Track"), "genre_id": 2}
    tech_track = {**_track(track_id=2, title="Tech Track"), "genre_id": 21}
    with patch("src.fetchers.volumo._get_json") as mock_get:
        mock_get.return_value = [_album(tracks=[bass_track, tech_track])]
        items = volumo.fetch(settings)

    assert len(items) == 1
    assert items[0].title == "Bass Track"
