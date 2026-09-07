"""config/settings.pool.yaml — the fetch configuration `publish-pool` runs on,
generated from the vendored taxonomy (tools/publish-pool-contract/taxonomy.yaml).

The Sunday run keeps its own config/settings.yaml: its genre lists are tuned for
one DJ's report and roll sub-genres up into coarse tags. The pool is the whole
taxonomy — every Beatport chart, Volumo genre, Bandcamp tag and SoundCloud
target the multi-tenant API knows about, with the fine genre id carried through
as each row's tag so taxonomy.fine_genres_for() can resolve it back.

Generating rather than hand-writing keeps the two in step: the taxonomy is a
founder decision recorded in the multi-tenant repo (ADR 0022), and
test_publisher_pool_settings.py fails the moment the committed file drifts from
what this module renders. Regenerate with `publish-pool --write-settings`.

Only `sources` (plus the `pool` block and `taxonomy_version`) come from here —
load_pool_settings() inherits Discord, data_dir, scoring and the rest from
settings.yaml, so a publish run reports and alerts exactly as the Sunday run does.
"""
import os

import yaml

from src.config import Settings, load_settings
from src.publisher.contract import CONTRACT_DIR
from src.publisher.taxonomy import Taxonomy

_TAXONOMY_PATH = os.path.join(CONTRACT_DIR, "taxonomy.yaml")

POOL_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "settings.pool.yaml",
)

_HEADER = (
    "# GENERATED from tools/publish-pool-contract/taxonomy.yaml v{version} by "
    "'tunefinder publish-pool --write-settings' — do not edit; edit the taxonomy "
    "in the multi-tenant repo and regenerate\n"
)

# The publisher's own knobs. They live in the generated file (not settings.yaml)
# so a publish run's cadence is one file with its sources; Settings.pool_* carry
# the same values as defaults for a settings.yaml that has no `pool:` block.
_POOL_DEFAULTS = {
    "batch_size": 200,
    "targets": ["dev"],
    "snapshot_retention_days": 14,
    "artist_weeks": 13,
    "lock_retry_seconds": 300,
    "lock_wait_max_seconds": 7200,
}


def _base_source(base: dict, name: str) -> dict:
    return (base.get("sources") or {}).get(name) or {}


def _beatport_slug_to_id() -> dict[str, int]:
    """Beatport genre ids, read from the taxonomy YAML directly — the Taxonomy
    dataclass keeps only slug -> fine genres, and the fetcher needs the id to
    build its chart URL."""
    with open(_TAXONOMY_PATH) as f:
        rows = yaml.safe_load(f)["beatport"]["genres"]
    return {row["slug"]: row["id"] for row in rows}


def generate_pool_settings(taxonomy: Taxonomy, base: dict) -> dict:
    """The pool fetch config: every taxonomy row as a fetcher target, with the
    per-source request knobs (sort, lookback, limits) carried over from `base`
    — the settings.yaml data — so the pool fetches the way the Sunday run does.

    Row order follows the taxonomy's. Each row's tag (`name` / `tf_tag`) is the
    fine genre id; Beatport's one two-fine row (breaks + uk-bass) uses the first,
    and the tag is a fallback only — the publisher resolves genres through
    taxonomy.fine_genres_for() on the raw slug/id/tag, not through this name.
    """
    slug_to_id = _beatport_slug_to_id()
    volumo_base = _base_source(base, "volumo")
    bandcamp_base = _base_source(base, "bandcamp")
    soundcloud_base = _base_source(base, "soundcloud")

    soundcloud_targets = []
    for row in taxonomy.soundcloud_targets:
        target = {"tf_tag": row["fine"]}
        for key in ("genres", "tags"):
            if key in row:
                target[key] = row[key]
        soundcloud_targets.append(target)

    sources = {
        "beatport": {
            "enabled": True,
            "genres": [
                {"name": fines[0], "slug": slug, "id": slug_to_id[slug]}
                for slug, fines in taxonomy.beatport_genres.items()
            ],
        },
        "volumo": {
            "enabled": True,
            "sort": volumo_base.get("sort", "purchase"),
            "curation": volumo_base.get("curation", "curated"),
            "lookback_days": volumo_base.get("lookback_days", 28),
            "limit_per_genre": volumo_base.get("limit_per_genre", 50),
            "genres": [
                {"name": fine, "id": volumo_id}
                for volumo_id, fine in taxonomy.volumo_genres.items()
            ],
        },
        "bandcamp": {
            "enabled": True,
            "count_per_tag": bandcamp_base.get("count_per_tag", 20),
            "tags": list(taxonomy.bandcamp_tags),
        },
        "soundcloud": {
            "enabled": True,
            "downloadable_only": soundcloud_base.get("downloadable_only", True),
            "include_gated_free": soundcloud_base.get("include_gated_free", True),
            "lookback_days": soundcloud_base.get("lookback_days", 28),
            "limit_per_target": soundcloud_base.get("limit_per_target", 50),
            "max_duration_minutes": soundcloud_base.get("max_duration_minutes", 15),
            "targets": soundcloud_targets,
        },
    }
    # Four sources, not nine. TuneFinder still carries fetchers for traxsource,
    # boomkat, bleep, resident_advisor and mixupload, switched off years ago as
    # unreliable; the pool never fetched them and CONTRACTS §8 never promised
    # them. Listing them here as `enabled: false` stated a nine-source picture
    # the product does not have, so they are simply absent: an unlisted source
    # is not fetched, and nothing downstream has to be told it is off.
    return {
        "taxonomy_version": taxonomy.version,
        "pool": dict(_POOL_DEFAULTS),
        "sources": sources,
    }


def render_pool_settings(data: dict) -> str:
    """The generated file's bytes: the do-not-edit header, then the data in
    insertion order (sort_keys=False keeps the taxonomy's row order readable
    next to the YAML it came from)."""
    return _HEADER.format(version=data["taxonomy_version"]) + yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True
    )


def load_pool_settings() -> Settings:
    """settings.yaml with its `sources` replaced by the generated pool file's,
    and that file's `pool` / `taxonomy_version` blocks added.

    Replaced, not merged: the publisher fetches exactly the pool's sources, so a
    source TuneFinder enables and the pool file does not is not fetched, and one
    the pool enables is fetched whatever settings.yaml says. Everything else —
    Discord channels, data_dir, scoring, alerts — is inherited unchanged.
    """
    # Settings deliberately exposes typed accessors rather than its tree; the
    # overlay needs the whole tree, so it reads it here and rebuilds a Settings.
    data = dict(load_settings()._data)
    with open(POOL_SETTINGS_PATH) as f:
        pool_data = yaml.safe_load(f) or {}
    data["sources"] = pool_data.get("sources", {})
    data["pool"] = pool_data.get("pool", {})
    data["taxonomy_version"] = pool_data.get("taxonomy_version")
    return Settings(data)
