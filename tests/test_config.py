"""Tests for config loading and Settings class."""
import pytest
from src.config import Settings
from src.pipeline.ranker import ScoringWeights


def test_settings_scoring_weights_returns_dataclass():
    """Verify that scoring_weights() returns a ScoringWeights instance."""
    settings = Settings({})
    weights = settings.scoring_weights()
    assert isinstance(weights, ScoringWeights)


def test_settings_scoring_weights_defaults_when_absent():
    """Verify that absent scoring block uses all defaults."""
    settings = Settings({})
    weights = settings.scoring_weights()
    assert weights.w_known_artist == 3.0
    assert weights.w_recurring == 2.0
    assert weights.recency_weeks == 4


def test_settings_scoring_weights_from_config():
    """Verify that scoring block in config overrides defaults."""
    config = {
        "scoring": {
            "w_known_artist": 5.0,
            "w_recurring": 3.5,
            "recency_weeks": 2,
        }
    }
    settings = Settings(config)
    weights = settings.scoring_weights()
    assert weights.w_known_artist == 5.0
    assert weights.w_recurring == 3.5
    assert weights.recency_weeks == 2
    # Other defaults should remain
    assert weights.w_label_base == 1.5


def test_settings_scoring_weights_partial_override():
    """Verify that partial config merges with defaults."""
    config = {
        "scoring": {
            "w_known_artist": 4.0,
        }
    }
    settings = Settings(config)
    weights = settings.scoring_weights()
    assert weights.w_known_artist == 4.0
    # All others should be defaults
    assert weights.w_recurring == 2.0
    assert weights.w_label_base == 1.5
    assert weights.fresh_days == 7


def test_settings_scoring_weights_unknown_keys_logged_and_ignored(caplog):
    """Verify that unknown keys in scoring config are logged and ignored."""
    config = {
        "scoring": {
            "w_known_artist": 3.0,
            "unknown_key": 99.0,
            "another_bad_key": "string",
        }
    }
    settings = Settings(config)
    weights = settings.scoring_weights()
    # Should successfully create weights ignoring the unknown keys
    assert weights.w_known_artist == 3.0
    # Check that warning was logged
    assert "Unknown scoring keys ignored" in caplog.text
    assert "unknown_key" in caplog.text


def test_settings_scoring_weights_all_fields_configurable():
    """Verify that all ScoringWeights fields can be configured."""
    config = {
        "scoring": {
            "w_known_artist": 2.0,
            "w_recurring": 1.5,
            "w_label_base": 1.0,
            "w_label_per_artist": 0.3,
            "label_artist_cap": 2,
            "w_cross_source_per": 0.4,
            "cross_source_cap": 3,
            "w_recency_penalty": 0.5,
            "recency_weeks": 3,
            "w_pool_age_per_week": 0.2,
            "pool_age_penalty_max": 1.0,
            "w_genre": 0.4,
            "genre_match_cap": 1,
            "w_fresh": 0.3,
            "fresh_days": 5,
            "w_chart_top": 1.0,
            "w_bandcamp": 0.8,
            "max_artist_score": 8.0,
            "recurring_threshold": 2,
        }
    }
    settings = Settings(config)
    weights = settings.scoring_weights()

    assert weights.w_known_artist == 2.0
    assert weights.w_recurring == 1.5
    assert weights.w_label_base == 1.0
    assert weights.w_label_per_artist == 0.3
    assert weights.label_artist_cap == 2
    assert weights.w_cross_source_per == 0.4
    assert weights.cross_source_cap == 3
    assert weights.w_recency_penalty == 0.5
    assert weights.recency_weeks == 3
    assert weights.w_pool_age_per_week == 0.2
    assert weights.pool_age_penalty_max == 1.0
    assert weights.w_genre == 0.4
    assert weights.genre_match_cap == 1
    assert weights.w_fresh == 0.3
    assert weights.fresh_days == 5
    assert weights.w_chart_top == 1.0
    assert weights.w_bandcamp == 0.8
    assert weights.max_artist_score == 8.0
    assert weights.recurring_threshold == 2


# ---------------------------------------------------------------------------
# artist_aliases() — issue #4
# ---------------------------------------------------------------------------

def test_artist_aliases_missing_file_returns_empty_dict_no_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("src.config._ALIASES_PATH", str(tmp_path / "aliases.yaml"))
    settings = Settings({})
    assert settings.artist_aliases() == {}
    assert "aliases" not in caplog.text.lower()


def test_artist_aliases_empty_file_returns_empty_dict(tmp_path, monkeypatch):
    path = tmp_path / "aliases.yaml"
    path.write_text("# no aliases configured yet\n")
    monkeypatch.setattr("src.config._ALIASES_PATH", str(path))
    settings = Settings({})
    assert settings.artist_aliases() == {}


def test_artist_aliases_inverts_canonical_to_alias_map(tmp_path, monkeypatch):
    path = tmp_path / "aliases.yaml"
    path.write_text("Calibre: [Dave Skinner, DRS & Calibre]\n")
    monkeypatch.setattr("src.config._ALIASES_PATH", str(path))
    settings = Settings({})
    assert settings.artist_aliases() == {
        "dave skinner": "calibre",
        "drs & calibre": "calibre",
    }


def test_artist_aliases_lowercases_and_strips(tmp_path, monkeypatch):
    path = tmp_path / "aliases.yaml"
    path.write_text("Calibre: [' Dave Skinner ']\n")
    monkeypatch.setattr("src.config._ALIASES_PATH", str(path))
    settings = Settings({})
    assert settings.artist_aliases() == {"dave skinner": "calibre"}


def test_artist_aliases_malformed_not_a_mapping_logs_warning_returns_empty(tmp_path, monkeypatch, caplog):
    path = tmp_path / "aliases.yaml"
    path.write_text("- just\n- a\n- list\n")
    monkeypatch.setattr("src.config._ALIASES_PATH", str(path))
    settings = Settings({})
    assert settings.artist_aliases() == {}
    assert "Malformed aliases file" in caplog.text


def test_artist_aliases_malformed_alias_value_not_a_list_logs_warning_returns_empty(tmp_path, monkeypatch, caplog):
    path = tmp_path / "aliases.yaml"
    path.write_text("Calibre: Dave Skinner\n")  # should be a list, not a bare string
    monkeypatch.setattr("src.config._ALIASES_PATH", str(path))
    settings = Settings({})
    assert settings.artist_aliases() == {}
    assert "Malformed aliases file" in caplog.text
