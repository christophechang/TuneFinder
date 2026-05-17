import json
from datetime import datetime, timedelta, timezone

import pytest

from src.pipeline.history import recent_recommended_artists


def _write_history(path, records):
    path.write_text(json.dumps(records))


def _rec(artist, days_ago):
    return {
        "artist": artist,
        "title": "t",
        "link": "",
        "source": "s",
        "recommended_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
        "report_id": "test",
    }


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def test_recency_includes_within_window(data_dir):
    _write_history(data_dir / "recommendation_history.json", [_rec("Sully", days_ago=3)])
    _write_history(data_dir / "mix_prep_history.json", [])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "sully" in result


def test_recency_excludes_outside_window(data_dir):
    _write_history(data_dir / "recommendation_history.json", [_rec("Sully", days_ago=60)])
    _write_history(data_dir / "mix_prep_history.json", [])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "sully" not in result


def test_recency_includes_mix_prep_history(data_dir):
    _write_history(data_dir / "recommendation_history.json", [])
    _write_history(data_dir / "mix_prep_history.json", [_rec("Calibre", days_ago=2)])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "calibre" in result


def test_recency_splits_collab_artists(data_dir):
    _write_history(data_dir / "recommendation_history.json",
                   [_rec("Bakey, Kasia", days_ago=1)])
    _write_history(data_dir / "mix_prep_history.json", [])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "bakey" in result
    assert "kasia" in result


def test_recency_handles_missing_files(data_dir):
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert result == set()
