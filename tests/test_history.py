import json
from datetime import datetime, timedelta, timezone

import pytest

from src.pipeline.history import (
    recent_recommended_artists,
    _record_to_dict,
    _dict_to_record,
    build_history_keys,
)
from src.models import RecommendationRecord


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


# ---------------------------------------------------------------------------
# RecommendationRecord round-trip with new fields
# ---------------------------------------------------------------------------

def _full_record(**overrides) -> RecommendationRecord:
    defaults = dict(
        artist="Calibre",
        title="New Dawn",
        link="https://beatport.com/1",
        source="beatport",
        recommended_at="2026-06-11T10:00:00+00:00",
        report_id="2026-W24",
        track_no=3,
        signal_codes=["known_artist", "label_match"],
        genre_tags=["dnb", "electronic"],
        score=4.5,
        label="Signature",
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


def test_round_trip_all_new_fields():
    r = _full_record()
    d = _record_to_dict(r)
    r2 = _dict_to_record(d)
    assert r2.track_no == 3
    assert r2.signal_codes == ["known_artist", "label_match"]
    assert r2.genre_tags == ["dnb", "electronic"]
    assert r2.score == 4.5
    assert r2.label == "Signature"
    assert r2.key == r.key


def test_legacy_dict_loads_with_defaults():
    legacy = {
        "artist": "Sully",
        "title": "Glasshouse",
        "link": "",
        "source": "bandcamp",
        "recommended_at": "2026-01-01T00:00:00+00:00",
        "report_id": "2026-W01",
    }
    r = _dict_to_record(legacy)
    assert r.track_no is None
    assert r.signal_codes == []
    assert r.genre_tags == []
    assert r.score is None
    assert r.label is None
    assert r.key == "sully||glasshouse"


def test_build_history_keys_identical_for_legacy_and_extended():
    legacy_dict = {
        "artist": "Sully",
        "title": "Glasshouse",
        "link": "",
        "source": "bandcamp",
        "recommended_at": "2026-01-01T00:00:00+00:00",
        "report_id": "2026-W01",
    }
    legacy_rec = _dict_to_record(legacy_dict)
    extended_rec = _full_record(artist="Sully", title="Glasshouse", link="", source="bandcamp",
                                recommended_at="2026-01-01T00:00:00+00:00", report_id="2026-W01")
    keys_legacy = build_history_keys([legacy_rec])
    keys_extended = build_history_keys([extended_rec])
    assert keys_legacy == keys_extended
