"""The publish-pool genre taxonomy (tools/publish-pool-contract/taxonomy.yaml):
8 families, 38 fine genres, and the per-source mappings that resolve a
SourceItem's raw genre data (whatever slug/id/tag the fetcher stored in
raw_metadata, or — for SoundCloud — genre_tags) to fine-genre ids.

Pure functions only — no IO beyond load_taxonomy() reading the one YAML file.
"""
import os
from dataclasses import dataclass, field

import yaml

from src.models import SourceItem
from src.publisher.contract import CONTRACT_DIR

_DEFAULT_TAXONOMY_PATH = os.path.join(CONTRACT_DIR, "taxonomy.yaml")


@dataclass(frozen=True)
class Taxonomy:
    version: int
    families: dict[str, str]                  # family id -> display name
    fine_genres: dict[str, str]                # fine genre id -> family id
    beatport_genres: dict[str, list[str]]      # Beatport genre slug -> fine ids
    beatport_sub_genres: dict[str, str]        # Beatport sub-genre slug -> fine id
    volumo_genres: dict[int, str]              # Volumo genre id -> fine id
    bandcamp_tags: dict[str, str]              # Bandcamp tag -> fine id
    soundcloud_targets: list[dict]             # the yaml rows, as written
    exclusions: dict[str, list[str]] = field(default_factory=dict)


def _check_fine(fine, row_description: str, known_fine_genres: dict) -> list[str]:
    """Normalise a row's `fine` value to a list (most rows carry one id; the
    Beatport breaks-breakbeat-uk-bass row carries two), raising ValueError
    naming the offending row if `fine` is missing or not a known fine-genre id."""
    fines = fine if isinstance(fine, list) else [fine]
    for fine_id in fines:
        if fine_id is None or fine_id not in known_fine_genres:
            raise ValueError(
                f"taxonomy.yaml: {row_description} has an unknown fine genre {fine_id!r}"
            )
    return fines


def load_taxonomy(path: str = _DEFAULT_TAXONOMY_PATH) -> Taxonomy:
    with open(path) as f:
        data = yaml.safe_load(f)

    families = {family_id: row["name"] for family_id, row in data["families"].items()}
    fine_genres = {
        fine_id: row["family"] for fine_id, row in data["fine_genres"].items()
    }

    beatport_genres: dict[str, list[str]] = {}
    for row in data["beatport"]["genres"]:
        slug = row["slug"]
        beatport_genres[slug] = _check_fine(
            row.get("fine"), f"beatport genre slug={slug!r}", fine_genres
        )

    beatport_sub_genres: dict[str, str] = {}
    for row in data["beatport"]["sub_genres"]:
        slug = row["slug"]
        fines = _check_fine(row.get("fine"), f"beatport sub_genre slug={slug!r}", fine_genres)
        beatport_sub_genres[slug] = fines[0]

    volumo_genres: dict[int, str] = {}
    for row in data["volumo"]["genres"]:
        volumo_id = row["id"]
        fines = _check_fine(row.get("fine"), f"volumo genre id={volumo_id!r}", fine_genres)
        volumo_genres[volumo_id] = fines[0]

    bandcamp_tags: dict[str, str] = {}
    for row in data["bandcamp"]["tags"]:
        tag = row["tag"]
        fines = _check_fine(row.get("fine"), f"bandcamp tag={tag!r}", fine_genres)
        bandcamp_tags[tag] = fines[0]

    soundcloud_targets: list[dict] = []
    for row in data["soundcloud"]["targets"]:
        _check_fine(row.get("fine"), f"soundcloud target fine={row.get('fine')!r}", fine_genres)
        soundcloud_targets.append(dict(row))

    exclusions = {
        family_id: list(excluded) for family_id, excluded in data.get("exclusions", {}).items()
    }

    return Taxonomy(
        version=data["version"],
        families=families,
        fine_genres=fine_genres,
        beatport_genres=beatport_genres,
        beatport_sub_genres=beatport_sub_genres,
        volumo_genres=volumo_genres,
        bandcamp_tags=bandcamp_tags,
        soundcloud_targets=soundcloud_targets,
        exclusions=exclusions,
    )


def fine_genres_for(item: SourceItem, taxonomy: Taxonomy) -> list[str]:
    """Resolve a SourceItem's fine genres from the source id/slug/tag its
    fetcher stored in raw_metadata (or, for SoundCloud, genre_tags). Sorted,
    unique; [] if the source is unrecognised or its raw genre data doesn't
    resolve to anything."""
    resolved: list[str] = []

    if item.source == "beatport":
        sub_genre_slug = item.raw_metadata.get("sub_genre_slug")
        if sub_genre_slug in taxonomy.beatport_sub_genres:
            resolved = [taxonomy.beatport_sub_genres[sub_genre_slug]]
        else:
            genre_slug = item.raw_metadata.get("genre_slug")
            resolved = taxonomy.beatport_genres.get(genre_slug, [])
    elif item.source == "volumo":
        volumo_genre_id = item.raw_metadata.get("volumo_genre_id")
        fine = taxonomy.volumo_genres.get(volumo_genre_id)
        resolved = [fine] if fine is not None else []
    elif item.source == "bandcamp":
        bandcamp_tag = item.raw_metadata.get("bandcamp_tag")
        fine = taxonomy.bandcamp_tags.get(bandcamp_tag)
        resolved = [fine] if fine is not None else []
    elif item.source == "soundcloud":
        # The pool settings put the fine id straight in tf_tag; sc_genre is
        # SoundCloud's own folksonomy and is ignored.
        resolved = [tag for tag in item.genre_tags if tag in taxonomy.fine_genres]

    return sorted(set(resolved))


def families_for(fine_genres: list[str], taxonomy: Taxonomy) -> list[str]:
    """The families a set of fine genres belong to. Sorted, unique."""
    return sorted({
        taxonomy.fine_genres[fine_genre]
        for fine_genre in fine_genres
        if fine_genre in taxonomy.fine_genres
    })
