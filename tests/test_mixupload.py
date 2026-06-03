from pathlib import Path
from unittest.mock import patch, MagicMock
from src.fetchers import mixupload

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fake_html(filename):
    return (FIXTURE_DIR / filename).read_text()


def _make_settings(enabled=True, period="month", targets=None):
    s = MagicMock()
    if targets is None:
        targets = [
            {"tf_tag": "house", "chart": "style/house"},
            {"tf_tag": "uk-bass", "genre": "UKBass"},
        ]
    s.get_source_config.return_value = {
        "enabled": enabled,
        "period": period,
        "targets": targets,
    }
    return s


def test_fetch_returns_empty_when_disabled():
    settings = _make_settings(enabled=False)
    assert mixupload.fetch(settings) == []


def test_fetch_chart_items():
    settings = _make_settings(targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert len(items) == 2
    first = items[0]
    assert first.source == "mixupload"
    assert first.artist == "Artist Name"
    assert first.title == "Track Title"
    assert first.link == "https://mixupload.com/track/artist-name-track-title-1234567"
    assert first.label is None  # label not present in chart card layout
    assert first.release_date == "2026-06-01"
    assert "house" in first.genre_tags
    assert first.raw_metadata["chart_position"] == 1
    assert first.raw_metadata["bpm"] == 128
    assert first.raw_metadata["key"] == "Am"


def test_fetch_chart_position_with_delta():
    settings = _make_settings(targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert items[1].raw_metadata["chart_position"] == 2  # strip the "+3" delta


def test_fetch_genre_tracks_mode_no_chart_position():
    settings = _make_settings(targets=[{"tf_tag": "uk-bass", "genre": "UKBass"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_genre_tracks_ukbass.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert len(items) == 1
    assert items[0].source == "mixupload"
    assert "uk-bass" in items[0].genre_tags
    assert "chart_position" not in items[0].raw_metadata


def test_fetch_genre_tracks_url_uses_genres_path():
    settings = _make_settings(targets=[{"tf_tag": "uk-bass", "genre": "UKBass"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_genre_tracks_ukbass.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            mixupload.fetch(settings)

    called_url = mock_get.call_args[0][0]
    assert "/genres/UKBass/tracks" in called_url
    assert "period" not in called_url  # period filter not applied to genre/tracks


def test_fetch_chart_url_exact():
    settings = _make_settings(period="month", targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            mixupload.fetch(settings)

    called_url = mock_get.call_args[0][0]
    assert called_url == "https://mixupload.com/charts/track/style/house?period=month"


def test_fetch_target_genre_filters():
    settings = _make_settings(targets=[
        {"tf_tag": "house", "chart": "style/house"},
        {"tf_tag": "house", "chart": "style-part/deep-house"},
        {"tf_tag": "house", "chart": "style-part/tech-house"},
        {"tf_tag": "house", "chart": "style-part/progressive-house"},
        {"tf_tag": "techno", "chart": "style/techno"},
    ])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings, target_genre="house")

    # Only 4 house targets fetched — techno skipped
    assert mock_get.call_count == 4
    assert all("house" in i.genre_tags for i in items)


def test_fetch_target_genre_no_match_returns_empty():
    settings = _make_settings(targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html"):
        items = mixupload.fetch(settings, target_genre="funk-soul-jazz")
    assert items == []


def test_date_normalisation():
    assert mixupload._parse_date("01.06.26") == "2026-06-01"
    assert mixupload._parse_date("28.05.26") == "2026-05-28"
    assert mixupload._parse_date("bad") is None


def test_parse_position_strips_delta():
    assert mixupload._parse_position("1") == 1
    assert mixupload._parse_position("2 +3") == 2
    assert mixupload._parse_position("10 -5") == 10
    assert mixupload._parse_position("bad") is None


def test_parse_key():
    assert mixupload._parse_key("KEY: Em") == "Em"
    assert mixupload._parse_key("KEY: Am") == "Am"
    assert mixupload._parse_key("bad") is None


def test_parse_bpm_none_on_bad_input():
    assert mixupload._parse_bpm("bad") is None
    assert mixupload._parse_bpm("") is None


def test_chart_period_stored_in_raw_metadata():
    settings = _make_settings(period="month", targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert items[0].raw_metadata["chart_period"] == "month"


def test_genre_tracks_no_chart_period():
    settings = _make_settings(targets=[{"tf_tag": "uk-bass", "genre": "UKBass"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_genre_tracks_ukbass.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert "chart_period" not in items[0].raw_metadata


def test_genre_tags_merged_from_track_card():
    """Both deep-house and tech-house map to 'house' — deduped to single tag."""
    settings = _make_settings(targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert items[0].genre_tags == ["house"]  # deep-house + tech-house both → house, deduped


def test_genre_tags_cross_genre_track():
    """Second fixture track is tagged deep-house + electronica → two TF tags."""
    settings = _make_settings(targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert set(items[1].genre_tags) == {"house", "electronica"}


def test_genre_tags_ukbass_card_has_target_tag():
    """Genre tracks page (gallery-cell layout) has no genre links; tag comes from tf_tag only."""
    settings = _make_settings(targets=[{"tf_tag": "uk-bass", "genre": "UKBass"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_genre_tracks_ukbass.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert "uk-bass" in items[0].genre_tags


def test_engagement_counts_parsed():
    settings = _make_settings(targets=[{"tf_tag": "house", "chart": "style/house"}])
    with patch("src.fetchers.mixupload.get_html") as mock_get:
        mock_get.return_value = _fake_html("mixupload_chart_house.html")
        with patch("src.fetchers.mixupload.polite_sleep"):
            items = mixupload.fetch(settings)

    assert items[0].raw_metadata["download_count"] == 42
    assert items[0].raw_metadata["stream_count"] == 310


def test_parse_count():
    assert mixupload._parse_count("42") == 42
    assert mixupload._parse_count("1.2k") == 1200
    assert mixupload._parse_count("310") == 310
    assert mixupload._parse_count("bad") is None
    assert mixupload._parse_count("") is None
