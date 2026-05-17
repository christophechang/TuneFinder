"""Shared pytest fixtures."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.models import ArtistProfile


@pytest.fixture
def sample_profiles():
    """A small profile dict used across ranker/report tests."""
    return {
        "Sully": ArtistProfile(
            name="Sully",
            play_count=4,
            genres_seen=["breaks", "uk-bass"],
            track_titles=["Swandive", "Glasshouse", "Cherry"],
        ),
        "Skee Mask": ArtistProfile(
            name="Skee Mask",
            play_count=2,
            genres_seen=["electronica", "breaks"],
            track_titles=["Rio Dembo"],
        ),
        "Calibre": ArtistProfile(
            name="Calibre",
            play_count=6,
            genres_seen=["dnb"],
            track_titles=["Mr Right On"],
        ),
    }
