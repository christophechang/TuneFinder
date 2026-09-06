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


# ---------------------------------------------------------------------------
# Beatport credentials
# ---------------------------------------------------------------------------


def test_beatport_credentials_from_env(monkeypatch):
    from src.config import Settings
    monkeypatch.setenv("BEATPORT_USERNAME", "dj_test")
    monkeypatch.setenv("BEATPORT_PASSWORD", "s3cret")
    s = Settings({})
    assert s.beatport_username == "dj_test"
    assert s.beatport_password == "s3cret"


def test_beatport_credentials_default_empty(monkeypatch):
    from src.config import Settings
    monkeypatch.delenv("BEATPORT_USERNAME", raising=False)
    monkeypatch.delenv("BEATPORT_PASSWORD", raising=False)
    s = Settings({})
    assert s.beatport_username == ""
    assert s.beatport_password == ""


def test_validate_warns_when_beatport_enabled_without_creds(monkeypatch, caplog):
    from src.config import Settings
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    monkeypatch.delenv("BEATPORT_USERNAME", raising=False)
    monkeypatch.delenv("BEATPORT_PASSWORD", raising=False)
    s = Settings({"sources": {"beatport": {"enabled": True}}})
    with caplog.at_level("WARNING"):
        s.validate()
    assert any("Beatport is enabled" in r.message for r in caplog.records)


def test_validate_silent_when_beatport_disabled(monkeypatch, caplog):
    from src.config import Settings
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    s = Settings({"sources": {"beatport": {"enabled": False}}})
    with caplog.at_level("WARNING"):
        s.validate()
    assert not any("Beatport is enabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SoundCloud credentials
# ---------------------------------------------------------------------------


def test_soundcloud_credentials_from_env(monkeypatch):
    from src.config import Settings
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_ID", "app-id")
    monkeypatch.setenv("SOUNDCLOUD_CLIENT_SECRET", "app-s3cret")
    s = Settings({})
    assert s.soundcloud_client_id == "app-id"
    assert s.soundcloud_client_secret == "app-s3cret"


def test_soundcloud_credentials_default_empty(monkeypatch):
    from src.config import Settings
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_ID", raising=False)
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_SECRET", raising=False)
    s = Settings({})
    assert s.soundcloud_client_id == ""
    assert s.soundcloud_client_secret == ""


def test_validate_warns_when_soundcloud_enabled_without_creds(monkeypatch, caplog):
    from src.config import Settings
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_ID", raising=False)
    monkeypatch.delenv("SOUNDCLOUD_CLIENT_SECRET", raising=False)
    s = Settings({"sources": {"soundcloud": {"enabled": True}}})
    with caplog.at_level("WARNING"):
        s.validate()
    assert any("SoundCloud is enabled" in r.message for r in caplog.records)


def test_validate_silent_when_soundcloud_disabled(monkeypatch, caplog):
    from src.config import Settings
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    s = Settings({"sources": {"soundcloud": {"enabled": False}}})
    with caplog.at_level("WARNING"):
        s.validate()
    assert not any("SoundCloud is enabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Free-download lane pipeline keys
# ---------------------------------------------------------------------------


def test_free_download_lane_defaults():
    from src.config import Settings
    s = Settings({})
    assert s.pipeline_free_download_sources == []
    assert s.pipeline_free_downloads_count == 5
    assert s.pipeline_mix_prep_free_downloads_count == 10
    assert s.pipeline_free_downloads_min_score == 0.0


def test_free_download_lane_from_config():
    from src.config import Settings
    s = Settings({"pipeline": {
        "free_download_sources": ["soundcloud", "hypeddit"],
        "free_downloads_count": 3,
        "mix_prep_free_downloads_count": 7,
        "free_downloads_min_score": 0.5,
    }})
    assert s.pipeline_free_download_sources == ["soundcloud", "hypeddit"]
    assert s.pipeline_free_downloads_count == 3
    assert s.pipeline_mix_prep_free_downloads_count == 7
    assert s.pipeline_free_downloads_min_score == 0.5


def test_scoring_weights_soundcloud_popularity_fields():
    from src.config import Settings
    s = Settings({"scoring": {"w_soundcloud_popularity": 0.5, "soundcloud_popularity_downloads": 10}})
    w = s.scoring_weights()
    assert w.w_soundcloud_popularity == 0.5
    assert w.soundcloud_popularity_downloads == 10
    defaults = Settings({}).scoring_weights()
    assert defaults.w_soundcloud_popularity == 0.25
    assert defaults.soundcloud_popularity_downloads == 50


def test_free_downloads_mode_count_default():
    from src.config import Settings
    s = Settings({})
    assert s.pipeline_free_downloads_mode_count == 30


def test_scoring_weights_reposts_default():
    from src.pipeline.ranker import ScoringWeights
    assert ScoringWeights().soundcloud_popularity_reposts == 25


# ---------------------------------------------------------------------------
# Pool publisher env and `pool:` block (M1d Task 3)
# ---------------------------------------------------------------------------


def _clear_pool_env(monkeypatch):
    from src.config import _POOL_ENV_VARS
    for key in _POOL_ENV_VARS + ["TUNEFINDER_POOL_TOKEN_URL"]:
        monkeypatch.delenv(key, raising=False)


def test_pool_env_vars_are_optional(monkeypatch):
    """The six pool names are never required — validate() must not fail on them."""
    from src.config import _POOL_ENV_VARS, _REQUIRED_ENV_VARS, Settings
    assert _POOL_ENV_VARS == [
        "TUNEFINDER_POOL_API_DEV",
        "TUNEFINDER_POOL_API_PROD",
        "TUNEFINDER_POOL_TENANT",
        "TUNEFINDER_POOL_CLIENT_ID",
        "TUNEFINDER_POOL_CLIENT_SECRET",
        "TUNEFINDER_POOL_SCOPE",
    ]
    assert not set(_POOL_ENV_VARS) & set(_REQUIRED_ENV_VARS)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("DISCORD_GUILD_ID", "x")
    _clear_pool_env(monkeypatch)
    Settings({}).validate()          # no raise


def test_pool_env_properties_read_env(monkeypatch):
    from src.config import Settings
    monkeypatch.setenv("TUNEFINDER_POOL_API_DEV", "https://tunefinder-api-dev.setfolio.app/")
    monkeypatch.setenv("TUNEFINDER_POOL_API_PROD", "https://tunefinder-api.setfolio.app")
    monkeypatch.setenv("TUNEFINDER_POOL_TENANT", "setfolioid.onmicrosoft.com")
    monkeypatch.setenv("TUNEFINDER_POOL_CLIENT_ID", "6dae376a")
    monkeypatch.setenv("TUNEFINDER_POOL_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("TUNEFINDER_POOL_SCOPE", "api://1c43c8d2/.default")
    s = Settings({})
    assert s.pool_api_url("dev") == "https://tunefinder-api-dev.setfolio.app"   # trailing / stripped
    assert s.pool_api_url("prod") == "https://tunefinder-api.setfolio.app"
    assert s.pool_tenant == "setfolioid.onmicrosoft.com"
    assert s.pool_client_id == "6dae376a"
    assert s.pool_client_secret == "s3cret"
    assert s.pool_scope == "api://1c43c8d2/.default"


def test_pool_env_properties_empty_when_unset(monkeypatch):
    from src.config import Settings
    _clear_pool_env(monkeypatch)
    s = Settings({})
    assert s.pool_api_url("dev") == ""
    assert s.pool_api_url("prod") == ""
    assert s.pool_tenant == ""
    assert s.pool_client_id == ""
    assert s.pool_client_secret == ""
    assert s.pool_scope == ""
    assert s.pool_token_url == ""


def test_pool_api_url_rejects_unknown_env(monkeypatch):
    from src.config import Settings
    with pytest.raises(ValueError):
        Settings({}).pool_api_url("staging")


def test_pool_token_url_derived_from_tenant(monkeypatch):
    from src.config import Settings
    _clear_pool_env(monkeypatch)
    monkeypatch.setenv("TUNEFINDER_POOL_TENANT", "setfolioid.onmicrosoft.com")
    assert Settings({}).pool_token_url == (
        "https://setfolioid.ciamlogin.com/setfolioid.onmicrosoft.com/oauth2/v2.0/token"
    )


def test_pool_token_url_override(monkeypatch):
    from src.config import Settings
    monkeypatch.setenv("TUNEFINDER_POOL_TENANT", "setfolioid.onmicrosoft.com")
    monkeypatch.setenv(
        "TUNEFINDER_POOL_TOKEN_URL",
        "https://setfolioid.ciamlogin.com/11111111-2222-3333-4444-555555555555/oauth2/v2.0/token",
    )
    assert Settings({}).pool_token_url == (
        "https://setfolioid.ciamlogin.com/11111111-2222-3333-4444-555555555555/oauth2/v2.0/token"
    )


def test_pool_block_defaults():
    from src.config import Settings
    s = Settings({})
    assert s.pool_batch_size == 200
    assert s.pool_targets == ["dev"]
    assert s.pool_snapshot_retention_days == 14
    assert s.pool_artist_weeks == 13
    assert s.pool_lock_retry_seconds == 300
    assert s.pool_lock_wait_max_seconds == 7200
    assert s.pool_taxonomy_version is None


def test_pool_block_from_config():
    from src.config import Settings
    s = Settings({
        "taxonomy_version": 1,
        "pool": {
            "batch_size": 50,
            "targets": ["dev", "prod"],
            "snapshot_retention_days": 30,
            "artist_weeks": 26,
            "lock_retry_seconds": 60,
            "lock_wait_max_seconds": 600,
        },
    })
    assert s.pool_batch_size == 50
    assert s.pool_targets == ["dev", "prod"]
    assert s.pool_snapshot_retention_days == 30
    assert s.pool_artist_weeks == 26
    assert s.pool_lock_retry_seconds == 60
    assert s.pool_lock_wait_max_seconds == 600
    assert s.pool_taxonomy_version == 1


def test_env_example_pool_lines_are_comments_not_values():
    """Copying .env.example to .env (the documented first step) must not set
    any pool variable.

    python-dotenv strips an inline `#` comment only when a value precedes it —
    `NAME=          # prose` with an empty value keeps the prose AS the value.
    Six pool names written that way would make check-config report SET for
    every one and pool_api_url("dev") return a comment string. So the prose
    goes on its own `#` line and the optional keys stay commented out, as the
    TUNEFINDER_WEB_* block already does.
    """
    import os

    from dotenv import dotenv_values

    from src.config import _POOL_ENV_VARS

    example = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example"
    )
    values = dotenv_values(example)

    for key in _POOL_ENV_VARS + ["TUNEFINDER_POOL_TOKEN_URL"]:
        value = values.get(key)
        assert not (value or "").startswith("#"), f"{key} parses as a comment string: {value!r}"
        assert not value, f"{key} is set by .env.example — check-config would report it SET"
