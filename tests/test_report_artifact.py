"""Structured report artifact — src/pipeline/report_artifact.py."""
from datetime import date

from src.models import Candidate, RecommendationSignal
from src.pipeline.report import report_order
from src.pipeline.report_artifact import (
    SCHEMA_VERSION,
    build_report_artifact,
    list_report_artifact_ids,
    load_report_artifact,
    report_artifact_path,
    write_report_artifact,
)

_TODAY = date(2026, 7, 10)
_GENERATED = "2026-07-10T09:00:00+00:00"


def _candidate(artist="Sully", title="New One", source="beatport", **kwargs):
    c = Candidate(
        artist=artist, title=title, link=f"https://example.com/{title}",
        source=source, label=kwargs.pop("label", "Astrophonica"),
        genre_tags=kwargs.pop("genre_tags", ["breaks"]),
        raw_metadata=kwargs.pop("raw_metadata", {}),
    )
    c.score = kwargs.pop("score", 5.0)
    c.familiarity_score = kwargs.pop("familiarity_score", 3.0)
    c.discovery_score = kwargs.pop("discovery_score", 2.0)
    c.signals = kwargs.pop("signals", [RecommendationSignal("known_artist", "You play Sully.")])
    return c


def _sections():
    return {
        "top_picks": [_candidate(title="Alpha"), _candidate(title="Beta", artist="Skee Mask")],
        "wildcards": [_candidate(title="Gamma", artist="Unknown", signals=[])],
    }


def _build(sections=None, **kwargs):
    return build_report_artifact(
        sections if sections is not None else _sections(),
        kwargs.pop("report_id", "2026-W28"),
        kwargs.pop("kind", "weekly"),
        kwargs.pop("stats", {"sources_fetched": 10}),
        today=_TODAY,
        generated_at=_GENERATED,
        **kwargs,
    )


def test_build_artifact_top_level_shape():
    artifact = _build()
    assert artifact["schema_version"] == SCHEMA_VERSION
    assert artifact["kind"] == "weekly"
    assert artifact["report_id"] == "2026-W28"
    assert artifact["generated_at"] == _GENERATED
    assert artifact["dry_run"] is False
    assert artifact["track_count"] == 3
    assert [s["key"] for s in artifact["sections"]] == ["top_picks", "wildcards"]
    assert artifact["sections"][0]["label"] == "Top Picks"


def test_build_artifact_track_numbers_match_report_order():
    sections = _sections()
    artifact = _build(sections)
    ordered = report_order(sections)
    flat = [t for s in artifact["sections"] for t in s["tracks"]]
    assert [t["track_no"] for t in flat] == list(range(1, len(ordered) + 1))
    assert [t["title"] for t in flat] == [c.title for c in ordered]


def test_build_artifact_track_payload_fields():
    sections = {"top_picks": [_candidate(
        title="Rich", raw_metadata={
            "bandcamp_album_id": 12345, "bpm": 172, "keysign": "Am",
            "chart_position": 7, "seen_on_sources": ["beatport", "volumo"],
        },
    )]}
    t = _build(sections)["sections"][0]["tracks"][0]
    assert t["key"] == "sully||rich"
    assert t["embed"] == {"type": "bandcamp", "album_id": 12345}
    assert t["bpm"] == 172.0
    assert t["camelot"] == "8A"
    assert t["key_raw"] == "Am"
    assert t["chart_position"] == 7
    assert t["seen_on_sources"] == ["beatport", "volumo"]
    assert t["signals"] == [{"code": "known_artist", "explanation": "You play Sully."}]
    assert t["score"] == 5.0 and t["familiarity_score"] == 3.0 and t["discovery_score"] == 2.0
    assert isinstance(t["reason"], str) and t["reason"]


def test_build_artifact_beatport_embed_fallback():
    sections = {"top_picks": [_candidate(raw_metadata={"beatport_id": 999})]}
    t = _build(sections)["sections"][0]["tracks"][0]
    assert t["embed"] == {"type": "beatport", "track_id": 999}


def test_build_artifact_no_embed_when_no_ids():
    t = _build()["sections"][0]["tracks"][0]
    assert t["embed"] is None


def test_build_artifact_mix_prep_filters_payload():
    filters = {"bpm_min": 170.0, "bpm_max": 180.0, "bpm_flex": True,
               "key_camelot": "8A", "description": "Filters: BPM 170–180 (±half/double) · key 8A±compat"}
    artifact = _build(kind="mix-prep", genre="dnb", filters=filters,
                      report_id="2026-W28-mix-prep-dnb")
    assert artifact["genre"] == "dnb"
    assert artifact["filters"] == filters


def test_write_load_roundtrip_and_listing(tmp_path):
    data_dir = str(tmp_path)
    artifact = _build()
    path = write_report_artifact(artifact, data_dir)
    assert path == report_artifact_path(data_dir, "2026-W28")
    assert load_report_artifact(data_dir, "2026-W28") == artifact
    assert load_report_artifact(data_dir, "missing") is None
    assert list_report_artifact_ids(data_dir) == ["2026-W28"]


def test_list_report_artifact_ids_empty_dir(tmp_path):
    assert list_report_artifact_ids(str(tmp_path)) == []
