"""CONTRACTS §7 — a day's fetched SourceItems turned into contract items.

One identity group (key_v2) becomes one item. The group's members are merged the
way the Sunday run merges duplicates — richest wins, genre tags unioned, embed
metadata backfilled — but with the publisher's longer backfill tuple, and on
copies, because the fetchers' items are also the corpus the rest of the run
reads and nothing here may mutate them.

Two rules of the merge are deliberately *not* the merged item's:

  * `fine_genres` is the union over the **members**. A Beatport track charting
    on two genre charts arrives as two rows with one slug each; the merged row
    keeps one slug and would lose the other family.
  * `preview` and `observation` are chosen by kind and by chart position over
    the members too — the richest member is not necessarily the one with a
    playable ref, nor the one that charted.

Everything the schema will not accept becomes null rather than an exception: a
blank Beatport release date, a BPM outside 40..300, a key string `to_camelot`
cannot parse. What cannot become null — a blank artist or title, no fine
genre, no usable source ref, an over-long key — makes the item **skipped with a
reason**, never raised, so one bad row never costs a run.

Pure functions, no IO, no network: nothing here imports `requests` or touches
`data/`.
"""
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date

from src.models import SourceItem
# Imported by their private names on purpose (as identity.py does): the
# publisher and the Sunday run must merge a duplicate group identically, and
# there is one implementation of "richest wins".
from src.pipeline.dedup import _MERGE_BACKFILL_KEYS, _richness
from src.pipeline.harmonic import to_camelot
from src.publisher import PUBLISHER_VERSION
from src.publisher.contract import check_item_relations
from src.publisher.identity import IDENTITY_VERSION, item_identity
from src.publisher.taxonomy import Taxonomy, families_for, fine_genres_for

# batch/manifest/artists all carry this; CONTRACTS §10 gives one release of
# overlap when it moves.
SCHEMA_VERSION = 1

# batch.schema.json's `source_name` enum, in its order. The manifest reports
# every one of them, so the status page can say `disabled` rather than nothing.
KNOWN_SOURCES = (
    "beatport",
    "bandcamp",
    "traxsource",
    "boomkat",
    "bleep",
    "resident_advisor",
    "mixupload",
    "volumo",
    "soundcloud",
)

# CONTRACTS §7's "extended in the publisher": dedup.py's tuple keeps what the
# report needs; the wire also needs the artwork, the preview ref, the catalogue
# numbers and the genre slugs, so a group whose richest member is (say) the
# SoundCloud row still publishes Beatport's ISRC. dedup.py's own tuple is
# untouched — the Sunday run's merge does not change.
PUBLISHER_BACKFILL_KEYS = _MERGE_BACKFILL_KEYS + (
    "artwork_url",
    "artwork_uuid",
    "sample_url",
    "isrc",
    "catalog_number",
    "version",
    "mix_name",
    "keysign",
    "genre_slug",
    "sub_genre_slug",
    "volumo_genre_id",
    "bandcamp_tag",
    "item_image_id",
)

# The raw_metadata key holding a source's own id for the sources it numbers.
# Anything else refs by its link.
_SOURCE_ID_KEYS = {
    "beatport": "beatport_id",
    "volumo": "volumo_track_id",
    "bandcamp": "bandcamp_album_id",
    "soundcloud": "soundcloud_id",
}

# The four counters and flags only SoundCloud carries (free-downloads mode).
_SOUNDCLOUD_SOURCE = "soundcloud"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CAMELOT_RE = re.compile(r"^(1[0-2]|[1-9])[AB]$")
_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r"(?:/Users|/var)/\S+")
# `requests`' own connection-pool exhaustion message names the host outright
# (no scheme, so `_URL_RE` misses it) and the request path after "with url:"
# (which is rarely under /Users or /var, so `_PATH_RE` misses it too).
_HOST_POOL_RE = re.compile(r"HTTPS?ConnectionPool\(host='[^']*', port=\d+\)")
_WITH_URL_RE = re.compile(r"with url: \S+")

_MAX_KEY_LENGTH = 512
_MAX_ERROR_LENGTH = 200


@dataclass(frozen=True)
class BuiltCorpus:
    """What one run's fetched items became: the items to post, and the ones
    that could not be posted, each with the API's own rejection reason."""

    items: list[dict]
    skipped: list[tuple[str, str]]


# ---------------------------------------------------------------------------
# Grouping and merging
# ---------------------------------------------------------------------------

def group_by_identity(items: list[SourceItem]) -> dict[str, list[SourceItem]]:
    """Fetched items grouped by `key_v2` — one group is one pool document.

    Insertion order is preserved so the group's member order is the fetch order:
    that is what "the first member carrying a chart position" means below.
    """
    groups: dict[str, list[SourceItem]] = {}
    for item in items:
        groups.setdefault(item_identity(item)[1], []).append(item)
    return groups


def merge_facts(members: list[SourceItem]) -> SourceItem:
    """One SourceItem carrying the group's richest facts.

    `dedup._merge_group`'s rules over `PUBLISHER_BACKFILL_KEYS`, applied to deep
    copies: the fetchers' items belong to the rest of the run and the publisher
    is a reader of them.

    Ties are broken by source name, lexically first — `_merge_rank`. The Sunday
    run can let fetch order settle a tie because its output is a report read
    once; the publisher's is a stored pool document, replaced by every later
    run, so a tie settled by fetch order would flip the same document's
    `granularity`, `primary_source` and release facts from day to day.
    """
    copies = deepcopy(members)
    best = min(copies, key=_merge_rank)

    all_genres: list[str] = []
    for item in copies:
        for tag in item.genre_tags:
            if tag not in all_genres:
                all_genres.append(tag)
    best.genre_tags = all_genres
    best.raw_metadata["seen_on_sources"] = sorted({item.source for item in copies})

    losers = [item for item in copies if item is not best]
    for key in PUBLISHER_BACKFILL_KEYS:
        if best.raw_metadata.get(key) is None:
            for loser in losers:
                value = loser.raw_metadata.get(key)
                if value is not None:
                    best.raw_metadata[key] = value
                    break

    return best


def source_ref(item: SourceItem) -> dict | None:
    """`{source, id, url}` for one member, or None when it has no link.

    The id is the source's own id where it numbers its catalogue, and the link
    otherwise — `id` is `minLength: 1` and a link is the only other thing every
    fetched item has. No link means no url, which means no ref at all.
    """
    link = (item.link or "").strip()
    if not link:
        return None
    id_key = _SOURCE_ID_KEYS.get(item.source)
    raw_id = item.raw_metadata.get(id_key) if id_key else None
    ref_id = str(raw_id) if raw_id is not None and str(raw_id) else link
    return {"source": item.source, "id": ref_id, "url": link}


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------

def preview_for(
    merged: SourceItem, members: list[SourceItem], checked_at: str
) -> dict | None:
    """The best playable ref the group carries, by kind: a Beatport sample, then
    Volumo's prelisten, then the SoundCloud widget, then a Bandcamp embed.

    Chosen over the members rather than over `merged`, because the richest
    member is not necessarily the one that can be played. `merged` is in the
    signature so every builder here takes the same pair; the preview is the
    group's, not the winner's.

    `eligible` is always true in M1d: nothing fetches the ref to check it (open
    point 6). Volumo's ref is the prelisten endpoint — the SPA appends its own
    `c` token, which is not ours to mint.
    """
    for member in members:
        if member.source == "beatport":
            sample_url = _text_or_none(member.raw_metadata.get("sample_url"))
            if sample_url:
                return _preview("beatport_sample", sample_url, checked_at)

    for member in members:
        if member.source == "volumo":
            track_id = _text_or_none(member.raw_metadata.get("volumo_track_id"))
            if track_id:
                return _preview(
                    "volumo_prelisten",
                    f"https://volumo.com/api/v1/tracks/{track_id}/prelisten.mp3",
                    checked_at,
                )

    for member in members:
        if member.source == _SOUNDCLOUD_SOURCE:
            link = _text_or_none(member.link)
            if link:
                return _preview("soundcloud_widget", link, checked_at)

    for member in members:
        if member.source == "bandcamp":
            album_id = _text_or_none(member.raw_metadata.get("bandcamp_album_id"))
            if album_id:
                return _preview("bandcamp_embed", album_id, checked_at)

    return None


def artwork_url_for(merged: SourceItem) -> str | None:
    """The source's own artwork url, or the one its id template builds.

    Beatport and SoundCloud send a url. Bandcamp and Volumo send an id and no
    url, so the url is built: Bandcamp's image server takes the image id and a
    size suffix (`_16` is the 700px square the API's card wants), and Volumo
    serves `/img/size/<width>x0/<uuid>.jpg` (144, 500, 600 and 1000 are the
    widths it answers; 500 is the card's).
    """
    artwork_url = _http_url_or_none(merged.raw_metadata.get("artwork_url"))
    if artwork_url:
        return artwork_url
    image_id = _text_or_none(merged.raw_metadata.get("item_image_id"))
    if image_id:
        return f"https://f4.bcbits.com/img/a{image_id}_16.jpg"
    artwork_uuid = _text_or_none(merged.raw_metadata.get("artwork_uuid"))
    if artwork_uuid:
        return f"https://volumo.com/img/size/500x0/{artwork_uuid}.jpg"
    return None


def release_date_or_none(text) -> str | None:
    """`YYYY-MM-DD` that is a real date, else None (Beatport sends `""`)."""
    candidate = _text_or_none(text)
    if not candidate or not _ISO_DATE_RE.match(candidate):
        return None
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def bpm_or_none(value) -> float | None:
    """A number in the schema's 40..300, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        bpm = float(value)
    except (TypeError, ValueError):
        return None
    if bpm != bpm or not 40 <= bpm <= 300:  # bpm != bpm catches NaN
        return None
    return bpm


def camelot_or_none(merged: SourceItem) -> str | None:
    """`to_camelot` over the raw key strings, kept only if it is a Camelot code.

    The pattern is the schema's: `to_camelot` is trusted to be right, not to be
    in range, and a value the API would refuse as `bad_camelot` is worth less
    than a null.
    """
    for text in _raw_key_texts(merged):
        camelot = to_camelot(text)
        if camelot and _CAMELOT_RE.match(camelot):
            return camelot
    return None


def key_raw_or_none(merged: SourceItem) -> str | None:
    """The source's key string as written — Beatport/SoundCloud `key`, Volumo
    `keysign` — kept alongside `camelot` so a disagreement can be traced."""
    return next(_raw_key_texts(merged), None)


def int_or_none(value) -> int | None:
    """A whole number >= 0, else None (the schema's `minimum: 0` counters)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def bool_or_none(value) -> bool | None:
    """A boolean, or None when the source said nothing at all."""
    return None if value is None else bool(value)


def summarise_error(text: str | None) -> str | None:
    """A fetch failure, fit to show on the public status page.

    The status page takes no credential and prints this verbatim, and `requests`
    puts the full request url in every HTTPError message — so urls and local
    paths go first, then whitespace, then the length. The API truncates at 500;
    200 is what a status line can read.
    """
    if not text:
        return None
    summary = _URL_RE.sub("<url>", str(text))
    summary = _PATH_RE.sub("<path>", summary)
    summary = _HOST_POOL_RE.sub("<host>", summary)
    summary = _WITH_URL_RE.sub("with url: <url>", summary)
    summary = " ".join(summary.split())
    if not summary:
        return None
    return summary[:_MAX_ERROR_LENGTH]


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

def build_items(
    items: list[SourceItem],
    taxonomy: Taxonomy,
    *,
    observed_on: date,
    seen_at: str,
) -> BuiltCorpus:
    """A day's fetched items as contract items, sorted by `key_v2`.

    Sorted because batch numbers must be stable across a replay: the same corpus
    posted twice has to put the same item in the same batch, and fetch order is
    whatever the sources answered with today.
    """
    built: list[dict] = []
    skipped: list[tuple[str, str]] = []

    for key_v2, members in group_by_identity(items).items():
        item, reason = _build_one(
            members, taxonomy, observed_on=observed_on, seen_at=seen_at
        )
        if reason is not None:
            skipped.append((key_v2, reason))
        else:
            built.append(item)

    built.sort(key=lambda item: item["key_v2"])
    return BuiltCorpus(items=built, skipped=skipped)


def batches(items: list[dict], size: int) -> list[list[dict]]:
    """`items` in chunks of at most `size` — the schema's `maxItems` per batch."""
    if size < 1:
        raise ValueError(f"batch size must be at least 1, got {size}")
    return [items[start : start + size] for start in range(0, len(items), size)]


def batch_payload(
    run_id: str, batch_no: int, items: list[dict], taxonomy_version: int
) -> dict:
    """One `POST /api/ingest/batch` body."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "batch_no": batch_no,
        "taxonomy_version": taxonomy_version,
        "identity_version": IDENTITY_VERSION,
        "items": items,
    }


def manifest_payload(
    run_id: str,
    started_at: str,
    completed_at: str,
    batches: int,
    per_source: dict,
) -> dict:
    """The `POST /api/ingest/manifest` body — the only thing that advances
    pool freshness, and only once every batch 1..batches is acknowledged."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "batches": batches,
        "started_at": started_at,
        "completed_at": completed_at,
        "per_source": per_source,
        "publisher_version": PUBLISHER_VERSION,
    }


def per_source_report(
    health: dict, fetch_switches: dict[str, bool], configured_enabled: set[str]
) -> dict:
    """The manifest's `per_source`, one entry per known source.

    A source that ran reports its count and — if it failed — a public summary of
    why. A source that did not run reports `enabled: false`, which is what lets
    the status page say **disabled** rather than **failed**: `enabled` is the
    fetch switch the publisher read at the start of the run, so both the API's
    switch and the operator's own settings turn it off.
    """
    report: dict[str, dict] = {}
    for name in KNOWN_SOURCES:
        entry = health.get(name)
        if entry is not None:
            report[name] = {
                "count": int_or_none(entry.get("count")) or 0,
                "error": summarise_error(entry.get("error")),
                "enabled": True,
            }
            continue
        enabled = name in configured_enabled and bool(fetch_switches.get(name, True))
        report[name] = {"count": 0, "error": None, "enabled": enabled}
    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_one(
    members: list[SourceItem],
    taxonomy: Taxonomy,
    *,
    observed_on: date,
    seen_at: str,
) -> tuple[dict | None, str | None]:
    """One group as `(item, None)`, or `(None, reason)` naming the API's own
    rejection reason — the publisher refuses what the API would refuse."""
    merged = merge_facts(members)
    key_v1, key_v2, version, granularity = item_identity(merged)

    # Both are `minLength: 1` on the wire, and a batch carrying one blank value
    # is refused whole — so the run would lose its manifest, and freshness with
    # it, over one row. `missing_artist_title` is the API's own reason name.
    artist = (merged.artist or "").strip()
    title = (merged.title or "").strip()
    if not artist or not title:
        return None, "missing_artist_title"

    if len(key_v2) > _MAX_KEY_LENGTH:
        return None, "key_too_long"

    fine_genres = sorted(
        {
            fine_genre
            for member in members
            for fine_genre in fine_genres_for(member, taxonomy)
        }
    )
    if not fine_genres:
        return None, "no_fine_genre"

    sources = _source_refs(members)
    if not sources:
        return None, "no_sources"

    # The primary is the richest member that actually produced a ref, not the
    # richest member: a fetcher that returned no link for its best row (Bandcamp
    # sends `item_url: ""`, and the archive replay defaults `link` to "") would
    # otherwise name a source the item does not carry, and the whole item would
    # be dropped as `bad_primary_source`.
    source_names = {ref["source"] for ref in sources}
    primary_source = _primary_source(members, source_names)
    charting = next(
        (
            member
            for member in members
            if member.source in source_names
            and _chart_position(member) is not None
        ),
        None,
    )
    soundcloud = _richest_member(members, _SOUNDCLOUD_SOURCE)

    item = {
        "key_v2": key_v2,
        "key_v1": key_v1,
        "artist": artist,
        "title": title,
        "version": version,
        "granularity": granularity,
        "families": families_for(fine_genres, taxonomy),
        "fine_genres": fine_genres,
        "release_name": _text_or_none(merged.release_name),
        "release_date": release_date_or_none(merged.release_date),
        "label": _text_or_none(merged.label),
        "sources": sources,
        "primary_source": primary_source,
        "bpm": bpm_or_none(merged.raw_metadata.get("bpm")),
        "camelot": camelot_or_none(merged),
        "key_raw": key_raw_or_none(merged),
        "download_count": _soundcloud_fact(soundcloud, "download_count", int_or_none),
        "reposts_count": _soundcloud_fact(soundcloud, "reposts_count", int_or_none),
        "free_download": _soundcloud_fact(soundcloud, "free_download", bool_or_none),
        "acquisition_url": _soundcloud_fact(
            soundcloud, "acquisition_url", _http_url_or_none
        ),
        "isrc": _text_or_none(merged.raw_metadata.get("isrc")),
        "catalog_number": _text_or_none(merged.raw_metadata.get("catalog_number")),
        "artwork_url": artwork_url_for(merged),
        "preview": preview_for(merged, members, seen_at),
        "observation": {
            "date": observed_on.isoformat(),
            "source": charting.source if charting else primary_source,
            "chart_position": _chart_position(charting) if charting else None,
            "seen_at": seen_at,
        },
    }

    # A self-check, not a filter: every rule below is one this builder already
    # keeps. If it ever fires, the item is wrong and the run says so rather than
    # letting the API find out.
    reason = check_item_relations(item)
    if reason is not None:
        return None, reason
    return item, None


def _source_refs(members: list[SourceItem]) -> list[dict]:
    """One ref per source name — the richest member's — sorted by source.

    Two entries naming the same source is `duplicate_source`; the README says to
    post the richest. Sorted so a replay of the same corpus sends the same list
    whatever order the fetchers answered in.
    """
    best_by_source: dict[str, tuple[int, dict]] = {}
    for member in members:
        ref = source_ref(member)
        if ref is None:
            continue
        current = best_by_source.get(member.source)
        richness = _richness(member)
        if current is None or richness > current[0]:
            best_by_source[member.source] = (richness, ref)
    return [ref for _, (_, ref) in sorted(best_by_source.items(), key=lambda kv: kv[0])]


def _merge_rank(item: SourceItem) -> tuple[int, str]:
    """The sort key for "richest wins", smallest first: most facts, then the
    lexically first source name. The second half is what makes the winner
    independent of the order the fetchers answered in."""
    return -_richness(item), item.source


def _primary_source(members: list[SourceItem], source_names: set[str]) -> str:
    """The richest member whose source made it into `sources`.

    `primary_source` must be one of the item's own sources (`bad_primary_source`),
    and a member with no link produces no ref — so the merge's winner is not
    always a legal answer.
    """
    return min(
        (member for member in members if member.source in source_names),
        key=_merge_rank,
    ).source


def _richest_member(members: list[SourceItem], source: str) -> SourceItem | None:
    candidates = [member for member in members if member.source == source]
    return min(candidates, key=_merge_rank) if candidates else None


def _soundcloud_fact(member: SourceItem | None, key: str, coerce):
    """A SoundCloud-only field, read off the SoundCloud member of the group.

    Null for every other source: `download_count` and friends are that store's
    numbers, and a Bandcamp release has no opinion on them.
    """
    return coerce(member.raw_metadata.get(key)) if member is not None else None


def _chart_position(member: SourceItem | None) -> int | None:
    """A position on a chart, or None — `chart_position` below 1 is
    `bad_observation`, and a source that does not rank sends null."""
    if member is None:
        return None
    position = int_or_none(member.raw_metadata.get("chart_position"))
    return position if position is not None and position >= 1 else None


def _raw_key_texts(merged: SourceItem):
    """The group's raw key strings, in the order the sources are trusted:
    Beatport/SoundCloud `key`, then Volumo `keysign`."""
    for key_name in ("key", "keysign"):
        text = _text_or_none(merged.raw_metadata.get(key_name))
        if text:
            yield text


def _preview(kind: str, ref: str, checked_at: str) -> dict:
    return {"kind": kind, "ref": ref, "eligible": True, "checked_at": checked_at}


def _text_or_none(value) -> str | None:
    """A non-empty stripped string, or None — the fetchers send `""` and 0 as
    freely as they send null."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _http_url_or_none(value) -> str | None:
    text = _text_or_none(value)
    return text if text and text.startswith("http") else None
