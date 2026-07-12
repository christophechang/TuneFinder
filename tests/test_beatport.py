from unittest.mock import MagicMock, patch

import pytest

from src.fetchers import beatport


@pytest.fixture(autouse=True)
def _no_sleep():
    """Stop the fetcher's polite_sleep(2.0) from actually sleeping during tests."""
    with patch("src.fetchers.beatport.polite_sleep"):
        yield


def _settings(enabled=True, genres=None):
    s = MagicMock()
    if genres is None:
        genres = [{"name": "dnb", "slug": "drum-bass", "id": 1}]
    s.get_source_config.return_value = {"enabled": enabled, "genres": genres}
    return s


def _track(track_id=29206235, name="Lock It", slug="lock-it", bpm=87,
           mix="Primate Remix", key="Eb Major", label="Wobbles & Waffles",
           genre_slug="drum-bass", publish_date="2026-06-26", isrc="GB1"):
    return {
        "id": track_id, "name": name, "slug": slug, "bpm": bpm, "mix_name": mix,
        "isrc": isrc, "publish_date": publish_date,
        "artists": [{"name": "Flowidus"}, {"name": "Loboski"}],
        "genre": {"slug": genre_slug},
        "key": {"name": key},
        "release": {"name": "Lock It (Primate Remix)", "label": {"name": label}},
    }


def _page(results, has_next=False):
    return {"count": len(results), "next": ("x" if has_next else None), "results": results}


def test_fetch_disabled_returns_empty():
    assert beatport.fetch(_settings(enabled=False)) == []


def test_fetch_parses_source_item():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([_track()])):
        items = beatport.fetch(_settings())
    assert len(items) == 1
    it = items[0]
    assert it.source == "beatport"
    assert it.artist == "Flowidus, Loboski"
    assert it.title == "Lock It"
    assert it.label == "Wobbles & Waffles"          # from release.label.name
    assert it.link == "https://www.beatport.com/track/lock-it/29206235"
    assert it.release_date == "2026-06-26"
    assert it.release_name == "Lock It (Primate Remix)"
    assert "dnb" in it.genre_tags
    md = it.raw_metadata
    assert md["beatport_id"] == 29206235
    assert md["bpm"] == 87
    assert md["chart_position"] == 1
    assert md["key"] == "Eb Major"                  # harmonic enrichment
    assert md["mix_name"] == "Primate Remix"
    assert md["isrc"] == "GB1"


def test_chart_position_is_rank_order():
    tracks = [_track(track_id=i, name=f"T{i}", slug=f"t{i}") for i in range(1, 4)]
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page(tracks)):
        items = beatport.fetch(_settings())
    assert [it.raw_metadata["chart_position"] for it in items] == [1, 2, 3]


def test_target_genre_filters():
    settings = _settings(genres=[
        {"name": "dnb", "slug": "drum-bass", "id": 1},
        {"name": "house", "slug": "house", "id": 5},
    ])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([])) as gj:
        beatport.fetch(settings, target_genre="dnb")
    # only the dnb genre (id 1) is requested
    assert all("/genres/1/" in call.args[0] for call in gj.call_args_list)


def test_target_genre_no_match_returns_empty():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token") as gt, \
         patch("src.fetchers.beatport._get_json") as gj:
        items = beatport.fetch(_settings(), target_genre="funk-soul-jazz")
    assert items == []
    gt.assert_not_called()
    gj.assert_not_called()


def test_partial_results_when_one_genre_fails():
    settings = _settings(genres=[
        {"name": "dnb", "slug": "drum-bass", "id": 1},
        {"name": "house", "slug": "house", "id": 5},
    ])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=[_page([_track()]), Exception("boom")]):
        items = beatport.fetch(settings)
    assert len(items) == 1  # genre 1 succeeded, genre 5 failed


def test_all_genres_fail_raises():
    settings = _settings(genres=[
        {"name": "dnb", "slug": "drum-bass", "id": 1},
        {"name": "house", "slug": "house", "id": 5},
    ])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=Exception("boom")):
        try:
            beatport.fetch(settings)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass


def test_auth_error_propagates():
    from src.fetchers.beatport_auth import BeatportAuthError
    with patch("src.fetchers.beatport.beatport_auth.get_access_token",
               side_effect=BeatportAuthError("no creds")):
        try:
            beatport.fetch(_settings())
            assert False, "expected BeatportAuthError"
        except BeatportAuthError:
            pass


def test_pagination_follows_next_and_stops_at_100():
    page1 = _page([_track(track_id=i, slug=f"t{i}") for i in range(1, 61)], has_next=True)
    page2 = _page([_track(track_id=i, slug=f"u{i}") for i in range(61, 121)], has_next=True)
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=[page1, page2]) as gj:
        items = beatport.fetch(_settings())
    assert len(items) == 100                                  # capped at chart size
    assert [it.raw_metadata["chart_position"] for it in items[:3]] == [1, 2, 3]  # order kept
    assert gj.call_args_list[1].args[0] == "x"               # 2nd call used page1's `next` (="x"), not a re-request


def test_merged_feed_uses_per_track_genre_slug():
    """breaks/uk-bass is one combined feed; tags come from each track's own slug."""
    settings = _settings(genres=[{"name": "breaks-uk-bass", "slug": "breaks-breakbeat-uk-bass", "id": 9}])
    track = _track(track_id=1, slug="b", genre_slug="breaks-breakbeat-uk-bass")
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([track])):
        items = beatport.fetch(settings)
    assert items[0].genre_tags == ["breaks", "uk-bass"]      # per-track slug → both tags, not the feed name
