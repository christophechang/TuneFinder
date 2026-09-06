"""Tests for src/publisher/pool_settings.py — the generated pool source config
(config/settings.pool.yaml) and the overlay that loads it.

Offline: the taxonomy and both settings files are read from the working tree,
never the network.
"""
import os

import pytest
import yaml

from src.config import _CONFIG_PATH
from src.publisher.contract import CONTRACT_DIR
from src.publisher.pool_settings import (
    POOL_SETTINGS_PATH,
    generate_pool_settings,
    load_pool_settings,
    render_pool_settings,
)
from src.publisher.taxonomy import load_taxonomy

_TAXONOMY_YAML = os.path.join(CONTRACT_DIR, "taxonomy.yaml")


def _base() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


@pytest.fixture(scope="module")
def generated() -> dict:
    return generate_pool_settings(load_taxonomy(), _base())


# ---------------------------------------------------------------------------
# Drift guard — the committed file is exactly what the generator produces
# ---------------------------------------------------------------------------

def test_committed_file_matches_generator(generated):
    with open(POOL_SETTINGS_PATH) as f:
        committed = f.read()
    assert committed == render_pool_settings(generated)


def test_render_carries_the_generated_header(generated):
    rendered = render_pool_settings(generated)
    assert rendered.startswith(
        "# GENERATED from tools/publish-pool-contract/taxonomy.yaml v1 by "
        "'tunefinder publish-pool --write-settings' — do not edit; edit the "
        "taxonomy in the multi-tenant repo and regenerate\n"
    )
    assert yaml.safe_load(rendered) == generated


# ---------------------------------------------------------------------------
# Every taxonomy row reaches the generated file
# ---------------------------------------------------------------------------

def test_generated_beatport_rows_cover_taxonomy(generated):
    taxonomy = load_taxonomy()
    with open(_TAXONOMY_YAML) as f:
        raw_rows = yaml.safe_load(f)["beatport"]["genres"]
    rows = generated["sources"]["beatport"]["genres"]

    assert len(rows) == 29
    assert [row["slug"] for row in rows] == [raw["slug"] for raw in raw_rows]
    assert [row["id"] for row in rows] == [raw["id"] for raw in raw_rows]
    assert set(row["slug"] for row in rows) == set(taxonomy.beatport_genres)
    # `name` is the fine genre; the one two-fine row (breaks + uk-bass) uses the first.
    assert {row["slug"]: row["name"] for row in rows}["breaks-breakbeat-uk-bass"] == "breaks-breakbeat"
    assert {row["slug"]: row["name"] for row in rows}["rb"] == "rnb"
    assert generated["sources"]["beatport"]["enabled"] is True


def test_generated_volumo_rows_cover_taxonomy(generated):
    taxonomy = load_taxonomy()
    rows = generated["sources"]["volumo"]["genres"]

    assert len(rows) == 24
    assert [row["id"] for row in rows] == list(taxonomy.volumo_genres)
    assert [row["name"] for row in rows] == list(taxonomy.volumo_genres.values())
    assert {row["id"]: row["name"] for row in rows}[18] == "downtempo"
    # The base file's fetch knobs carry over unchanged.
    volumo = generated["sources"]["volumo"]
    base_volumo = _base()["sources"]["volumo"]
    assert volumo["sort"] == base_volumo["sort"]
    assert volumo["curation"] == base_volumo["curation"]
    assert volumo["lookback_days"] == base_volumo["lookback_days"]
    assert volumo["limit_per_genre"] == base_volumo["limit_per_genre"]
    assert volumo["enabled"] is True


def test_generated_bandcamp_tags(generated):
    taxonomy = load_taxonomy()
    bandcamp = generated["sources"]["bandcamp"]

    assert bandcamp["tags"] == list(taxonomy.bandcamp_tags)
    assert len(bandcamp["tags"]) == 17
    assert "future-garage" in bandcamp["tags"]
    assert bandcamp["count_per_tag"] == _base()["sources"]["bandcamp"]["count_per_tag"]
    assert bandcamp["enabled"] is True


def test_generated_soundcloud_targets(generated):
    taxonomy = load_taxonomy()
    soundcloud = generated["sources"]["soundcloud"]
    targets = soundcloud["targets"]

    assert len(targets) == 12
    # tf_tag is the fine genre id — that is what fine_genres_for() resolves against.
    assert [t["tf_tag"] for t in targets] == [row["fine"] for row in taxonomy.soundcloud_targets]
    assert {"tf_tag": "uk-bass", "tags": "uk bass,ukbass"} in targets
    assert {"tf_tag": "techno-raw-deep", "genres": "techno"} in targets
    base_soundcloud = _base()["sources"]["soundcloud"]
    for key in ("downloadable_only", "include_gated_free", "lookback_days",
                "limit_per_target", "max_duration_minutes"):
        assert soundcloud[key] == base_soundcloud[key]
    assert soundcloud["enabled"] is True


def test_disabled_sources_listed_disabled(generated):
    for name in ("traxsource", "boomkat", "bleep", "resident_advisor", "mixupload"):
        assert generated["sources"][name] == {"enabled": False}


def test_pool_block_and_taxonomy_version(generated):
    assert generated["taxonomy_version"] == load_taxonomy().version
    assert generated["pool"] == {
        "batch_size": 200,
        "targets": ["dev"],
        "snapshot_retention_days": 14,
        "artist_weeks": 13,
        "lock_retry_seconds": 300,
        "lock_wait_max_seconds": 7200,
    }


# ---------------------------------------------------------------------------
# load_pool_settings — sources replaced, everything else inherited
# ---------------------------------------------------------------------------

def test_load_pool_settings_overlays_sources_only(tmp_path, monkeypatch):
    base_path = tmp_path / "settings.yaml"
    base_path.write_text(yaml.safe_dump({
        "discord": {"alert_channel": "pool-alerts", "report_channel": "music-research"},
        "data_dir": str(tmp_path / "data"),
        "sources": {"traxsource": {"enabled": True}, "beatport": {"enabled": False}},
    }, sort_keys=False))

    pool_path = tmp_path / "settings.pool.yaml"
    pool_path.write_text(render_pool_settings(generate_pool_settings(load_taxonomy(), _base())))

    monkeypatch.setattr("src.config._CONFIG_PATH", str(base_path))
    monkeypatch.setattr("src.publisher.pool_settings.POOL_SETTINGS_PATH", str(pool_path))

    settings = load_pool_settings()

    # The pool file's sources win outright — TuneFinder's own switches are gone.
    assert settings.source_enabled("traxsource") is False
    assert settings.source_enabled("beatport") is True
    assert len(settings.get_source_config("beatport")["genres"]) == 29
    # Everything outside `sources` is inherited from settings.yaml.
    assert settings.discord_alert_channel == "pool-alerts"
    assert settings.data_dir == str(tmp_path / "data")
    # And the pool block / taxonomy version come from the generated file.
    assert settings.pool_batch_size == 200
    assert settings.pool_targets == ["dev"]
    assert settings.pool_taxonomy_version == load_taxonomy().version
