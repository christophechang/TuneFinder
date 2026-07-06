"""Tests for cross-source deduplication and _merge_group backfill."""
import pytest

from src.models import Candidate, RecommendationRecord, SourceItem, Track
from src.pipeline.dedup import (
    _MERGE_BACKFILL_KEYS,
    _merge_group,
    deduplicate_source_items,
    filter_history,
    filter_known,
    make_dedup_key,
)
from src.pipeline.history import build_history_keys
from src.pipeline.profile import build_known_track_keys


def _item(source, artist="Artist", title="Title", label=None, release_date=None, raw_metadata=None):
    return SourceItem(
        source=source,
        artist=artist,
        title=title,
        link=f"https://{source}.example.com/track",
        label=label,
        release_date=release_date,
        genre_tags=[],
        raw_metadata=raw_metadata or {},
    )


# ---------------------------------------------------------------------------
# _merge_group backfill
# ---------------------------------------------------------------------------

def test_winner_values_not_overwritten():
    winner = _item("beatport", raw_metadata={"beatport_id": 99, "bpm": 140})
    loser = _item("volumo", raw_metadata={"beatport_id": 1, "bpm": 999, "volumo_track_id": 42})
    # winner has higher richness via label
    winner.label = "Lab"
    merged = _merge_group([winner, loser])
    assert merged.raw_metadata["beatport_id"] == 99
    assert merged.raw_metadata["bpm"] == 140


def test_missing_keys_backfilled_from_losers():
    winner = _item("beatport", label="Lab", raw_metadata={"beatport_id": 10})
    loser = _item("volumo", raw_metadata={"volumo_track_id": 55, "volumo_album_id": 77, "keysign": "Am"})
    merged = _merge_group([winner, loser])
    assert merged.raw_metadata["volumo_track_id"] == 55
    assert merged.raw_metadata["volumo_album_id"] == 77
    assert merged.raw_metadata["keysign"] == "Am"
    assert merged.raw_metadata["beatport_id"] == 10  # winner's own value preserved


def test_non_allowlisted_keys_not_copied():
    winner = _item("beatport", label="Lab", raw_metadata={"beatport_id": 10})
    loser = _item("volumo", raw_metadata={"secret_field": "should_not_copy", "volumo_track_id": 5})
    merged = _merge_group([winner, loser])
    assert "secret_field" not in merged.raw_metadata
    assert merged.raw_metadata["volumo_track_id"] == 5


def test_single_item_group_no_backfill_error():
    item = _item("beatport", raw_metadata={"beatport_id": 7})
    merged = _merge_group([item])
    assert merged.raw_metadata["beatport_id"] == 7
    # seen_on_sources set even for single-item groups (existing behaviour)
    assert "seen_on_sources" in merged.raw_metadata


def test_bandcamp_album_id_backfilled():
    winner = _item("beatport", label="Lab", raw_metadata={"beatport_id": 20})
    loser = _item("bandcamp", raw_metadata={"bandcamp_album_id": 12345})
    merged = _merge_group([winner, loser])
    assert merged.raw_metadata["bandcamp_album_id"] == 12345


def test_backfill_keys_allowlist_exhaustive():
    # All expected keys present in the allowlist constant
    for key in ("beatport_id", "volumo_track_id", "volumo_album_id",
                "bandcamp_album_id", "bpm", "key", "keysign"):
        assert key in _MERGE_BACKFILL_KEYS


# ---------------------------------------------------------------------------
# deduplicate_source_items integration
# ---------------------------------------------------------------------------

def test_dedup_merges_cross_source_and_backfills():
    bp = _item("beatport", artist="Sully", title="Skyline",
               label="Metalheadz", raw_metadata={"beatport_id": 500})
    bc = _item("bandcamp", artist="Sully", title="Skyline",
               raw_metadata={"bandcamp_album_id": 9999})
    merged = deduplicate_source_items([bp, bc])
    assert len(merged) == 1
    m = merged[0]
    assert m.raw_metadata["beatport_id"] == 500
    assert m.raw_metadata["bandcamp_album_id"] == 9999
    assert sorted(m.raw_metadata["seen_on_sources"]) == ["bandcamp", "beatport"]


# ===========================================================================
# Remix-aware track identity (issue #9)
# ===========================================================================

# ---------------------------------------------------------------------------
# Flag OFF — regression guard. These hardcode the CURRENT legacy output so any
# accidental change to the flag-off path fails loudly, and assert the default
# equals remix_aware=False for a broad matrix.
# ---------------------------------------------------------------------------

_LEGACY_EXPECTED = [
    # (artist, title, expected legacy key)
    ("Calibre", "New Dawn", "calibre||new dawn"),
    ("Calibre", "New Dawn (Original Mix)", "calibre||new dawn"),
    ("Calibre", "New Dawn (Extended Mix)", "calibre||new dawn"),
    ("Calibre", "New Dawn (Radio Edit)", "calibre||new dawn"),
    ("Calibre", "New Dawn (Calibre Remix)", "calibre||new dawn"),
    ("Calibre", "New Dawn [Break Remix]", "calibre||new dawn"),
    ("Calibre", "New Dawn (VIP)", "calibre||new dawn"),
    ("Calibre", "New Dawn (Extended Remix)", "calibre||new dawn"),
    ("Sully", "Skyline (feat. Jabu)", "sully||skyline (feat. jabu)"),
    ("A & B", "Title", "a, b||title"),
]


@pytest.mark.parametrize("artist,title,expected", _LEGACY_EXPECTED)
def test_flag_off_matches_current_values(artist, title, expected):
    # Hardcoded expected values pin the legacy path.
    assert make_dedup_key(artist, title) == expected
    # Default arg is byte-identical to explicit remix_aware=False.
    assert make_dedup_key(artist, title, remix_aware=False) == expected


_FLAG_OFF_BROAD = [
    ("Calibre", "New Dawn (Instrumental)"),
    ("Calibre", "New Dawn (Dub)"),
    ("Calibre", "New Dawn (Vocal Mix)"),
    ("Calibre", "New Dawn (Club Mix)"),
    ("Calibre", "New Dawn (Album Version)"),
    ("Calibre", "New Dawn (Bootleg)"),
    ("Calibre", "New Dawn (Reprise)"),
    ("Calibre", "New Dawn (Calibre Flip)"),
    ("Sully", "Skyline (feat. Jabu) (Calibre Remix)"),
    ("A x B", "Title (Break's Deep Mix)"),
]


@pytest.mark.parametrize("artist,title", _LEGACY_EXPECTED_INPUTS := [(a, t) for a, t, _ in _LEGACY_EXPECTED] + _FLAG_OFF_BROAD)
def test_default_equals_explicit_false(artist, title):
    assert make_dedup_key(artist, title) == make_dedup_key(artist, title, remix_aware=False)


# ---------------------------------------------------------------------------
# Flag ON — classification matrix (generic merges vs named distinct)
# ---------------------------------------------------------------------------

# Every generic tag must produce the SAME key as the bare title (merge).
_GENERIC_MERGE_TITLES = [
    "New Dawn",
    "New Dawn (Original Mix)",
    "New Dawn (Extended Mix)",
    "New Dawn (Extended)",
    "New Dawn (Radio Edit)",
    "New Dawn (Radio Version)",
    "New Dawn (Club Mix)",
    "New Dawn (Album Version)",
    "New Dawn (EP Version)",
    "New Dawn (Vocal Mix)",
    "New Dawn (Instrumental)",
    "New Dawn (Dub)",
    "New Dawn (Extended Remix)",   # generic modifier + keyword → empty name → merge
    "New Dawn (Reprise)",
]


@pytest.mark.parametrize("title", _GENERIC_MERGE_TITLES)
def test_flag_on_generic_versions_merge(title):
    bare = make_dedup_key("Calibre", "New Dawn", remix_aware=True)
    assert make_dedup_key("Calibre", title, remix_aware=True) == bare == "calibre||new dawn"


# (title, expected qualified key) for named remixes.
_NAMED_EXPECTED = [
    ("New Dawn (Calibre Remix)", "calibre||new dawn||rmx:calibre"),
    ("New Dawn (Calibre remix)", "calibre||new dawn||rmx:calibre"),   # case-insensitive
    ("New Dawn [Calibre Remix]", "calibre||new dawn||rmx:calibre"),   # bracket form
    ("New Dawn (Break Remix)", "calibre||new dawn||rmx:break"),
    ("New Dawn (VIP)", "calibre||new dawn||rmx:vip"),
    ("New Dawn (VIP Mix)", "calibre||new dawn||rmx:vip"),
    ("New Dawn (Calibre Flip)", "calibre||new dawn||rmx:calibre"),
    ("New Dawn (Calibre Refix)", "calibre||new dawn||rmx:calibre"),
    ("New Dawn (Calibre Remake)", "calibre||new dawn||rmx:calibre"),
    ("New Dawn (Break's Deep Mix)", "calibre||new dawn||rmx:break's deep"),
    ("New Dawn (Extended Calibre Remix)", "calibre||new dawn||rmx:calibre"),  # generic modifier stripped
    ("New Dawn (  Calibre   Remix )", "calibre||new dawn||rmx:calibre"),      # whitespace collapse
]


@pytest.mark.parametrize("title,expected", _NAMED_EXPECTED)
def test_flag_on_named_remix_qualified(title, expected):
    assert make_dedup_key("Calibre", title, remix_aware=True) == expected


def test_flag_on_named_distinct_from_original_and_each_other():
    orig = make_dedup_key("Calibre", "New Dawn", remix_aware=True)
    calibre = make_dedup_key("Calibre", "New Dawn (Calibre Remix)", remix_aware=True)
    break_ = make_dedup_key("Calibre", "New Dawn (Break Remix)", remix_aware=True)
    vip = make_dedup_key("Calibre", "New Dawn (VIP)", remix_aware=True)
    assert len({orig, calibre, break_, vip}) == 4          # all distinct
    assert calibre != orig and break_ != orig and vip != orig


def test_flag_on_bracket_equals_paren():
    assert (make_dedup_key("Calibre", "New Dawn [Calibre Remix]", remix_aware=True)
            == make_dedup_key("Calibre", "New Dawn (Calibre Remix)", remix_aware=True))


def test_flag_on_extended_remix_merges_with_original():
    assert (make_dedup_key("Calibre", "New Dawn (Extended Remix)", remix_aware=True)
            == make_dedup_key("Calibre", "New Dawn", remix_aware=True))


def test_flag_on_feat_and_remix_combined():
    # feat credit is preserved in the base (legacy behaviour keeps parenthesised
    # feat), remix qualifier is appended; the feat-only version stays distinct
    # from the remix and both differ from a plain-title match.
    feat_only = make_dedup_key("Sully", "Skyline (feat. Jabu)", remix_aware=True)
    feat_remix = make_dedup_key("Sully", "Skyline (feat. Jabu) (Calibre Remix)", remix_aware=True)
    assert feat_only == "sully||skyline (feat. jabu)"
    assert feat_remix == "sully||skyline (feat. jabu)||rmx:calibre"
    assert feat_only != feat_remix
    # Un-parenthesised feat is stripped in both, remix still qualifies.
    assert (make_dedup_key("Sully", "Skyline feat. Jabu (Calibre Remix)", remix_aware=True)
            == "sully||skyline||rmx:calibre")


# ---------------------------------------------------------------------------
# filter_known / filter_history with remix-awareness
# ---------------------------------------------------------------------------

def _cand(artist, title):
    return Candidate(artist=artist, title=title, link="", source="beatport")


def test_filter_known_remix_aware_original_does_not_block_remix():
    # Known set built remix-aware from an owned ORIGINAL.
    known = build_known_track_keys([Track(artist="Calibre", title="New Dawn")], remix_aware=True)
    cands = [
        _cand("Calibre", "New Dawn"),                    # owned original → blocked
        _cand("Calibre", "New Dawn (Original Mix)"),     # generic version → blocked
        _cand("Calibre", "New Dawn (Break Remix)"),      # named remix → NOT blocked
    ]
    kept = filter_known(cands, known, remix_aware=True)
    kept_titles = [c.title for c in kept]
    assert "New Dawn (Break Remix)" in kept_titles
    assert "New Dawn" not in kept_titles
    assert "New Dawn (Original Mix)" not in kept_titles


def test_filter_known_remix_aware_owned_remix_stays_conservative():
    # Owning a NAMED remix. build_known_track_keys(remix_aware=True) emits BOTH the
    # remix-aware key AND the legacy key, so a known remix still blocks the original
    # / generic versions (via the legacy key) — the deliberately conservative
    # backward-compat choice (never resurface a track under the flag). It does NOT
    # block a DIFFERENT named remix, which keys separately.
    known = build_known_track_keys(
        [Track(artist="Calibre", title="New Dawn (Calibre Remix)")], remix_aware=True
    )
    cands = [
        _cand("Calibre", "New Dawn"),                    # original → blocked (legacy key)
        _cand("Calibre", "New Dawn (Calibre Remix)"),    # owned remix → blocked
        _cand("Calibre", "New Dawn (Break Remix)"),      # different remix → NOT blocked
    ]
    kept = {c.title for c in filter_known(cands, known, remix_aware=True)}
    assert kept == {"New Dawn (Break Remix)"}


def test_filter_history_remix_aware_owned_remix_blocks_different_remix_not():
    # Same conservative semantics for history: a recommended named remix blocks
    # itself and (via the legacy key) the original, but a different named remix of
    # the same title is still free to surface.
    hist = build_history_keys(
        [RecommendationRecord(artist="Calibre", title="New Dawn (Calibre Remix)",
                              link="", source="beatport", recommended_at="2026-01-01T00:00:00+00:00",
                              report_id="2026-W01")],
        remix_aware=True,
    )
    kept = {c.title for c in filter_history(
        [_cand("Calibre", "New Dawn"),
         _cand("Calibre", "New Dawn (Calibre Remix)"),
         _cand("Calibre", "New Dawn (Break Remix)")],
        hist, remix_aware=True,
    )}
    assert kept == {"New Dawn (Break Remix)"}


# ---------------------------------------------------------------------------
# Backward compatibility — old-style records/known-tracks block their exact
# match even under the flag.
# ---------------------------------------------------------------------------

def test_build_history_keys_backward_compat_blocks_old_style():
    # An old record stored as "Title (Original Mix)" written under the legacy
    # regime. Under the flag ON, its exact old-style (generic) match is still blocked.
    rec = RecommendationRecord(artist="Sully", title="Glasshouse (Original Mix)",
                               link="", source="bandcamp",
                               recommended_at="2026-01-01T00:00:00+00:00", report_id="2026-W01")
    keys_on = build_history_keys([rec], remix_aware=True)
    keys_off = build_history_keys([rec], remix_aware=False)
    # Legacy key present under both regimes (superset guarantee).
    assert keys_off <= keys_on
    # A fresh "Glasshouse" (generic) is still blocked under the flag.
    kept = filter_history([_cand("Sully", "Glasshouse")], keys_on, remix_aware=True)
    assert kept == []


def test_build_known_track_keys_flag_on_superset_of_legacy():
    tracks = [Track(artist="Calibre", title="New Dawn (Calibre Remix)")]
    off = build_known_track_keys(tracks, remix_aware=False)
    on = build_known_track_keys(tracks, remix_aware=True)
    assert off <= on                                     # legacy key retained
    assert "calibre||new dawn||rmx:calibre" in on        # plus the remix-aware key
    assert "calibre||new dawn" in on                     # legacy (feat/version-stripped)


# ---------------------------------------------------------------------------
# deduplicate_source_items with the flag ON
# ---------------------------------------------------------------------------

def test_dedup_flag_on_generic_merge_named_split():
    items = [
        _item("beatport", artist="Calibre", title="New Dawn (Original Mix)"),
        _item("volumo", artist="Calibre", title="New Dawn (Extended Mix)"),
        _item("bandcamp", artist="Calibre", title="New Dawn (Break Remix)"),
    ]
    merged = deduplicate_source_items(items, remix_aware=True)
    by_key = {make_dedup_key(m.artist, m.title, remix_aware=True): m for m in merged}
    assert len(merged) == 2
    # Original + Extended merged with all sources recorded.
    generic = by_key["calibre||new dawn"]
    assert sorted(generic.raw_metadata["seen_on_sources"]) == ["beatport", "volumo"]
    # Named remix stands alone with its own source list.
    remix = by_key["calibre||new dawn||rmx:break"]
    assert remix.raw_metadata["seen_on_sources"] == ["bandcamp"]


def test_dedup_flag_off_named_remix_still_merges():
    # Guard: with the flag OFF, the named remix merges with the original (today's
    # behaviour) — proving the split is gated entirely on the flag.
    items = [
        _item("beatport", artist="Calibre", title="New Dawn"),
        _item("bandcamp", artist="Calibre", title="New Dawn (Break Remix)"),
    ]
    assert len(deduplicate_source_items(items, remix_aware=False)) == 1
    assert len(deduplicate_source_items(items)) == 1  # default
