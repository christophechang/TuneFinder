from unittest.mock import MagicMock, patch

import pytest

from src.fetchers import beatport


@pytest.fixture(autouse=True)
def _no_sleep():
    """Stop the fetcher's polite_sleep(2.0) from actually sleeping during tests."""
    with patch("src.fetchers.beatport.polite_sleep"):
        yield


def _settings(enabled=True, genres=None, **extra):
    s = MagicMock()
    if genres is None:
        genres = [{"name": "dnb", "slug": "drum-bass", "id": 1}]
    s.get_source_config.return_value = {"enabled": enabled, "genres": genres, **extra}
    return s


_LIQUID = {"name": "liquid", "slug": "liquid", "id": 5, "genre_id": 1}
_JUMP_UP = {"name": "neurofunk-jump-up", "slug": "jump-up", "id": 6, "genre_id": 1}


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


# ---------------------------------------------------------------------------
# Taste-seeded search (feedback loop spec, Slice C)
# ---------------------------------------------------------------------------

def _search_track(name="New Cut", artist="Om Unit", label="Metalheadz", track_id=999):
    return {
        "id": track_id, "name": name, "slug": "new-cut", "bpm": 170, "mix_name": "Original Mix",
        "isrc": "GB2", "publish_date": "2026-08-01",
        "artists": [{"name": artist}],
        "genre": {"slug": "drum-bass"},
        "key": {"name": "F Minor"},
        "release": {"name": "New Cut EP", "label": {"name": label}},
    }


def _seeded_get_json(url, session):
    if "/catalog/search/" in url:
        # fuzzy search: right artist + an unrelated stray result
        return {"tracks": [_search_track(), _search_track(name="Stray", artist="Someone Else", track_id=1000)]}
    return _page([])


def test_seeded_search_matches_artist_and_tags(tmp_path):
    settings = _settings(genres=[])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_seeded_get_json):
        items = beatport.fetch(settings, seed_queries=["om unit"])
    assert len(items) == 1
    assert items[0].artist == "Om Unit"
    assert items[0].raw_metadata["seeded_by"] == "om unit"
    assert items[0].raw_metadata["chart_position"] is None


def test_seeded_search_matches_label_seed(tmp_path):
    settings = _settings(genres=[])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_seeded_get_json):
        items = beatport.fetch(settings, seed_queries=["metalheadz"])
    # both results carry the Metalheadz label → both match the label seed
    assert len(items) == 2
    assert all(i.raw_metadata["seeded_by"] == "metalheadz" for i in items)


def test_seeded_search_failure_does_not_raise(tmp_path):
    settings = _settings(genres=[])
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=RuntimeError("boom")):
        items = beatport.fetch(settings, seed_queries=["om unit"])
    assert items == []


def test_seeded_never_runs_for_genre_targeted_fetch(tmp_path):
    settings = _settings()
    calls = []

    def _get(url, session):
        calls.append(url)
        return _page([_track()])

    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_get):
        beatport.fetch(settings, target_genre="dnb", seed_queries=["om unit"])
    assert not any("search" in u for u in calls)


# ---------------------------------------------------------------------------
# publish-pool raw_metadata (M1d Task 3) — additive keys the payload builder reads
# ---------------------------------------------------------------------------

def _pool_track():
    """A chart row carrying every field the publisher reads. Shapes taken from
    the v4 track object: `genre`/`sub_genre` are {id, slug, name}, every image
    is {id, uri, dynamic_uri} — the track's own is the 1500×250 banner render,
    the release's the square cover — and the catalogue number sits on `release`."""
    return {
        **_track(),
        "genre": {"id": 9, "slug": "breaks-breakbeat-uk-bass"},
        "sub_genre": {"id": 66, "slug": "jungle"},
        "sample_url": "https://geo-samples.beatport.com/track/abc.LOFI.mp3",
        "image": {
            "id": 1,
            "uri": "https://geo-media.beatport.com/image_size/1500x250/19283746.png",
            "dynamic_uri": "https://geo-media.beatport.com/image_size/{w}x{h}/19283746.png",
        },
        "release": {
            "name": "Lock It (Primate Remix)",
            "label": {"name": "Wobbles & Waffles"},
            "catalog_number": "WAFFLE001",
            "image": {
                "id": 2,
                "uri": "https://geo-media.beatport.com/image_size/1400x1400/a404f8cc.jpg",
                "dynamic_uri": "https://geo-media.beatport.com/image_size/{w}x{h}/a404f8cc.jpg",
            },
        },
    }


def test_raw_metadata_carries_genre_sub_genre_sample_and_artwork():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([_pool_track()])):
        items = beatport.fetch(_settings())
    md = items[0].raw_metadata
    assert md["genre_slug"] == "breaks-breakbeat-uk-bass"
    assert md["genre_id"] == 9
    assert md["sub_genre_slug"] == "jungle"
    assert md["sample_url"] == "https://geo-samples.beatport.com/track/abc.LOFI.mp3"
    # The release's cover at 500×500 — never the track's banner (image id 1).
    assert md["artwork_url"] == "https://geo-media.beatport.com/image_size/500x500/a404f8cc.jpg"
    assert md["catalog_number"] == "WAFFLE001"


def test_artwork_falls_back_to_the_release_images_fixed_render():
    row = _pool_track()
    del row["release"]["image"]["dynamic_uri"]
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([row])):
        items = beatport.fetch(_settings())
    assert items[0].raw_metadata["artwork_url"] == "https://geo-media.beatport.com/image_size/1400x1400/a404f8cc.jpg"


def test_artwork_none_without_a_release_image_even_when_the_track_has_a_banner():
    row = _pool_track()
    del row["release"]["image"]
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([row])):
        items = beatport.fetch(_settings())
    assert items[0].raw_metadata["artwork_url"] is None


def test_raw_metadata_pool_keys_none_when_fields_missing():
    """The legacy fixture has no sub_genre/sample_url/image/catalog_number —
    every added key must read None, never raise."""
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", return_value=_page([_track()])):
        items = beatport.fetch(_settings())
    md = items[0].raw_metadata
    assert md["sub_genre_slug"] is None
    assert md["sample_url"] is None
    assert md["artwork_url"] is None
    assert md["catalog_number"] is None
    assert md["genre_id"] is None
    assert md["genre_slug"] == "drum-bass"


# ---------------------------------------------------------------------------
# Sub-genres (publish-pool): recent releases per sub-genre, not a chart
# ---------------------------------------------------------------------------

def _liquid_track(track_id, **kw):
    raw = _track(track_id=track_id, slug=f"l{track_id}", **kw)
    raw["sub_genre"] = {"id": 5, "slug": "liquid"}
    return raw


def _route(chart=None, sub=None, fail_sub=()):
    """_get_json stand-in: chart URLs get `chart`, sub-genre URLs get `sub[id]`."""
    def get(url, session):
        if "/catalog/tracks/" in url:
            sub_id = int(url.split("sub_genre_id=")[1].split("&")[0])
            if sub_id in fail_sub:
                raise Exception("boom")
            return _page((sub or {}).get(sub_id, []))
        return _page(chart or [])
    return get


def test_sub_genre_url_is_recent_releases_of_that_sub_genre():
    from datetime import date as real_date

    class _Today(real_date):
        @classmethod
        def today(cls):
            return real_date(2026, 10, 22)

    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport.date", _Today), \
         patch("src.fetchers.beatport._get_json", side_effect=_route()) as gj:
        beatport.fetch(_settings(genres=[], sub_genres=[_LIQUID]))
    assert [call.args[0] for call in gj.call_args_list] == [
        "https://api.beatport.com/v4/catalog/tracks/?sub_genre_id=5&genre_id=1"
        "&publish_date=2026-09-24:2026-10-22&order_by=-publish_date&per_page=50"
    ]


def test_sub_genre_knobs_override_defaults_and_limit_caps_at_100():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route()) as gj:
        beatport.fetch(_settings(genres=[], sub_genres=[_LIQUID],
                                 sub_genre_lookback_days=7, sub_genre_limit=500))
    url = gj.call_args_list[0].args[0]
    assert url.endswith("&per_page=100")
    start, end = url.split("publish_date=")[1].split("&")[0].split(":")
    from datetime import date
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 7


def test_sub_genre_items_carry_slug_and_no_chart_position():
    sub = {5: [_liquid_track(i) for i in (101, 102)]}
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route(chart=[_track()], sub=sub)):
        items = beatport.fetch(_settings(sub_genres=[_LIQUID]))
    assert len(items) == 3
    chart, *liquid = items
    assert chart.raw_metadata["chart_position"] == 1
    assert [it.raw_metadata["beatport_id"] for it in liquid] == [101, 102]
    assert all(it.raw_metadata["sub_genre_slug"] == "liquid" for it in liquid)
    assert all(it.raw_metadata["chart_position"] is None for it in liquid)


def test_sub_genre_items_resolve_to_their_fine_genre():
    from src.publisher.taxonomy import fine_genres_for, load_taxonomy
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route(sub={5: [_liquid_track(7)]})):
        items = beatport.fetch(_settings(genres=[], sub_genres=[_LIQUID]))
    assert fine_genres_for(items[0], load_taxonomy()) == ["liquid"]


def test_one_sub_genre_failing_keeps_the_rest_and_does_not_raise():
    sub = {6: [_liquid_track(9)]}
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json",
               side_effect=_route(chart=[_track()], sub=sub, fail_sub=(5,))):
        items = beatport.fetch(_settings(sub_genres=[_LIQUID, _JUMP_UP]))
    assert [it.raw_metadata["beatport_id"] for it in items] == [29206235, 9]


def test_sub_genre_failures_do_not_trip_the_all_genres_fail_safe():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route(fail_sub=(5,))):
        assert beatport.fetch(_settings(genres=[], sub_genres=[_LIQUID])) == []


def test_every_sub_genre_failing_logs_an_error(caplog):
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json",
               side_effect=_route(chart=[_track()], fail_sub=(5, 6))):
        items = beatport.fetch(_settings(sub_genres=[_LIQUID, _JUMP_UP]))
    assert len(items) == 1
    assert any(r.levelname == "ERROR" and "all 2 sub-genre fetches failed" in r.getMessage()
               for r in caplog.records)


def test_one_sub_genre_succeeding_logs_no_error(caplog):
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json",
               side_effect=_route(sub={6: [_liquid_track(9)]}, fail_sub=(5,))):
        beatport.fetch(_settings(genres=[], sub_genres=[_LIQUID, _JUMP_UP]))
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_sub_genre_rows_missing_ids_are_skipped():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route()) as gj:
        beatport.fetch(_settings(genres=[], sub_genres=[{"slug": "liquid", "id": 5}]))
    gj.assert_not_called()


def test_genre_targeted_fetch_never_runs_sub_genres():
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route()) as gj:
        beatport.fetch(_settings(sub_genres=[_LIQUID]), target_genre="dnb")
    assert all("/catalog/tracks/" not in call.args[0] for call in gj.call_args_list)


def test_no_sub_genres_key_means_charts_only():
    """The Sunday run's settings.yaml has no sub_genres: its fetch is unchanged."""
    with patch("src.fetchers.beatport.beatport_auth.get_access_token", return_value="T"), \
         patch("src.fetchers.beatport._get_json", side_effect=_route(chart=[_track()])) as gj:
        beatport.fetch(_settings())
    assert [call.args[0] for call in gj.call_args_list] == [
        "https://api.beatport.com/v4/catalog/genres/1/top/100/?per_page=100"
    ]
