import pytest

from src.fetchers.catalog import _clean_artist, fetch_all_mixes, fetch_all_tracks
from src.pipeline.profile import _dict_to_profile


# --- _clean_artist ---

def test_clean_artist_strips_dot_space():
    assert _clean_artist("15. Zero T") == "Zero T"


def test_clean_artist_strips_paren_space():
    assert _clean_artist("3) Foo") == "Foo"


def test_clean_artist_leaves_808_state():
    assert _clean_artist("808 State") == "808 State"


def test_clean_artist_leaves_2_bad_mice():
    assert _clean_artist("2 Bad Mice") == "2 Bad Mice"


def test_clean_artist_leaves_65daysofstatic():
    assert _clean_artist("65daysofstatic") == "65daysofstatic"


def test_clean_artist_requires_separator():
    # "15 Zero T" — no dot or paren — must NOT be stripped
    assert _clean_artist("15 Zero T") == "15 Zero T"


# --- _dict_to_profile tolerates legacy associated_labels key ---

def test_dict_to_profile_ignores_associated_labels():
    d = {
        "name": "Calibre",
        "play_count": 12,
        "genres_seen": ["dnb"],
        "associated_labels": ["Signature"],
        "track_titles": ["Falling"],
    }
    p = _dict_to_profile(d)
    assert p.name == "Calibre"
    assert p.play_count == 12
    assert p.track_titles == ["Falling"]


# --- catalog.user_url required (issue #12 — no hardcoded default URL) ---

class _EmptyUrlSettings:
    testing_use_fixtures = False
    catalog_user_url = ""


def test_fetch_all_tracks_empty_user_url_raises_clean_error():
    with pytest.raises(ValueError, match="catalog.user_url not configured"):
        fetch_all_tracks(_EmptyUrlSettings())


def test_fetch_all_mixes_empty_user_url_raises_clean_error():
    with pytest.raises(ValueError, match="catalog.user_url not configured"):
        fetch_all_mixes(_EmptyUrlSettings())
