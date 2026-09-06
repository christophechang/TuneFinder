"""CONTRACTS §1 identity — the two keys every published item carries.

- `key_v1` = `normalise_artist(artist)||normalise_title(title)`, TuneFinder's legacy
  key. It exists for matching only (a library row with no version information at all
  is ambiguous and is matched on this key).
- `key_v2` = `key_v1` plus `||rmx:<name>` when the version is named. It is the
  identity: the pool document id, the dedup group, the mark key, the history key.

Version extraction happens per source, before dedup, because `make_dedup_key` sees
only artist and title. Beatport's `mix_name` and Volumo's `version` are the
catalogue's own field, so the field wins and the title's parenthetical is dropped.
Rekordbox's `Remixer` is hand-typed and disagrees with the title on 14 of the 81 rows
that carry it (spike S1a), so there the title wins and the field is a guarded
fallback: used only when the title carries no named version and the value is neither
generic nor domain-like.

The classification rules are not reimplemented here: `_classify_version`,
`_strip_generic_modifiers`, `_GENERIC_VERSIONS`, `_NAMED_RE` and `_PAREN_GROUP_RE`
are imported from `src.pipeline.dedup` by their private names on purpose. The
publisher and the Sunday run must answer "is this a named remix?" identically, and
there is one implementation of that answer; a copy here would drift.

The cross-runtime oracle for this module is `tests/fixtures/identity/cases.json`
(spike S1b): the TypeScript library parser and the .NET engine reproduce the same
keys from the same 70 rows.
"""
import re

from src.models import SourceItem
from src.pipeline.dedup import (
    _GENERIC_VERSIONS,
    _NAMED_RE,
    _PAREN_GROUP_RE,
    _classify_version,
    _strip_generic_modifiers,
    make_dedup_key,
    normalise_artist,
    normalise_title,
)

# Bumped whenever the rules below change; stamped on pool items, exclusion sets and
# crates so mixed versions never match each other silently.
#   1 — TuneFinder v0.18.0's title-only `make_dedup_key(..., remix_aware=True)`.
#   2 — CONTRACTS §1 as implemented here: per-source version fields, the guarded
#       Remixer fallback, and the S1a classifier fixes (trailing year, modifiers
#       stripped anywhere).
IDENTITY_VERSION = 2

# Sources whose own catalogue supplies the version, and the raw_metadata field it
# arrives in. For everything else the version is parsed from the title.
_CATALOGUE_VERSION_FIELD = {"beatport": "mix_name", "volumo": "version"}
# Sources whose items are releases rather than tracks (CONTRACTS §1).
_RELEASE_GRANULARITY_SOURCES = {"bandcamp"}

# A hand-typed version field that is really a download site: "dbox.pro",
# "www.electronicfresh.com", "freednb.com / musikmp3.ucoz.com".
_DOMAIN_LIKE_RE = re.compile(r"www\.|http|\.[a-z]{2,}\b", re.IGNORECASE)


def classify_version(text: str | None) -> str | None:
    """`rmx:<name>` for a named version, None for a generic or empty one."""
    if not text:
        return None
    collapsed = " ".join(text.split()).lower()
    if not collapsed:
        return None
    return _classify_version(collapsed)


def is_domain_like(text: str) -> bool:
    """True when a version value is a URL or a domain rather than a name."""
    return bool(_DOMAIN_LIKE_RE.search(text))


def _title_named_version(title: str) -> tuple[str | None, str | None]:
    """The last *named* parenthetical of `title`.

    Returns `(inner text in its original casing, qualifier)`, or `(None, None)` when
    the title carries no named version — no parenthetical, or only generic ones.
    """
    inner_text: str | None = None
    qualifier: str | None = None
    for match in _PAREN_GROUP_RE.finditer(title):
        found = classify_version(match.group(1))
        if found is not None:
            inner_text = " ".join(match.group(1).split())
            qualifier = found
    return inner_text, qualifier


def _hand_typed_qualifier(value: str) -> str | None:
    """The qualifier for a hand-typed version field used as a fallback.

    Generic and domain-like values give nothing. A value carrying a remix keyword
    goes through the classifier, so "Extended Remix" is as generic here as it is in a
    title. A value with no keyword at all is the remixer's name, so "Calibre" and
    "Calibre Remix" both give `rmx:calibre`; a value that is nothing but modifier
    words ("Extended Vocal") gives nothing.
    """
    collapsed = " ".join(value.split()).lower()
    if not collapsed or collapsed in _GENERIC_VERSIONS or is_domain_like(collapsed):
        return None
    if _NAMED_RE.match(collapsed):
        return _classify_version(collapsed)
    name = _strip_generic_modifiers(collapsed)
    return f"rmx:{name}" if name else None


def identity_keys(
    artist: str,
    title: str,
    version: str | None = None,
    *,
    version_is_catalogue: bool = True,
) -> tuple[str, str]:
    """`(key_v1, key_v2)` for one item.

    `version_is_catalogue=True` (Beatport `mix_name`, Volumo `version`): the field
    wins — the qualifier comes from it and the title's own parentheticals are
    stripped by `normalise_title`. A missing field falls back to the title rule.

    `version_is_catalogue=False` (Rekordbox `Remixer`, or anything parsed from the
    title with no field at all): the title wins, so `key_v2` is exactly
    `make_dedup_key(artist, title, remix_aware=True)`; the field is used only when
    the title carries no named version and the value survives the guards above.
    """
    key_v1 = f"{normalise_artist(artist)}||{normalise_title(title)}"
    field = version.strip() if version else ""

    if version_is_catalogue and field:
        qualifier = classify_version(field)
        return key_v1, f"{key_v1}||{qualifier}" if qualifier else key_v1

    key_v2 = make_dedup_key(artist, title, remix_aware=True)
    if field and _title_named_version(title)[1] is None:
        qualifier = _hand_typed_qualifier(field)
        if qualifier:
            key_v2 = f"{key_v1}||{qualifier}"
    return key_v1, key_v2


def item_identity(item: SourceItem) -> tuple[str, str, str | None, str]:
    """`(key_v1, key_v2, version, granularity)` for a fetched item.

    `version` is what the payload sends: the catalogue field when the source has one,
    otherwise the inner text of the title's last named parenthetical in its original
    casing, otherwise None.
    """
    field_name = _CATALOGUE_VERSION_FIELD.get(item.source)
    catalogue_version: str | None = None
    if field_name:
        raw = item.raw_metadata.get(field_name)
        if isinstance(raw, str) and raw.strip():
            catalogue_version = raw.strip()

    key_v1, key_v2 = identity_keys(
        item.artist,
        item.title,
        catalogue_version,
        version_is_catalogue=field_name is not None,
    )
    version = catalogue_version or _title_named_version(item.title)[0]
    granularity = "release" if item.source in _RELEASE_GRANULARITY_SOURCES else "track"
    return key_v1, key_v2, version, granularity
