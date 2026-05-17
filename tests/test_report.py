import src.pipeline.report as report_mod
from src.models import ArtistProfile, Candidate, RecommendationSignal


class _Settings:
    def __init__(self):
        self.data_dir = "data"


def _make_candidate(artist="Sully", title="Cherry", signals=None, **kw):
    return Candidate(
        artist=artist, title=title, link="", source="beatport",
        signals=signals or [],
        **kw,
    )


def test_enrich_reasons_includes_prior_titles_for_known_artist(monkeypatch, sample_profiles):
    captured = {}

    def fake_call_stage1(prompt, system, settings, temperature=None):
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(report_mod, "call_stage1", fake_call_stage1)

    cand = _make_candidate(
        artist="Sully",
        title="Cherry",
        label="Astrophonica",
        genre_tags=["breaks"],
        raw_metadata={"chart_position": 3, "seen_on_sources": ["beatport"]},
        signals=[RecommendationSignal(code="known_artist", explanation="x")],
    )
    report_mod._enrich_reasons([cand], _Settings(), profiles=sample_profiles)

    assert captured["temperature"] == 0.3
    assert "Swandive" in captured["prompt"]
    assert "Glasshouse" in captured["prompt"]
    assert '"prior_titles_sample"' in captured["prompt"]


def test_enrich_reasons_empty_prior_titles_for_unknown_artist(monkeypatch):
    captured = {}
    monkeypatch.setattr(report_mod, "call_stage1",
                        lambda p, s, st, temperature=None: captured.setdefault("prompt", p) or "[]")
    cand = _make_candidate(artist="Nobody", title="Unknown", signals=[])
    report_mod._enrich_reasons([cand], _Settings(), profiles={})
    assert '"prior_titles_sample": []' in captured["prompt"]


def test_enrich_reasons_includes_cross_source_count(monkeypatch):
    captured = {}
    monkeypatch.setattr(report_mod, "call_stage1",
                        lambda p, s, st, temperature=None: captured.setdefault("prompt", p) or "[]")
    cand = _make_candidate(raw_metadata={"seen_on_sources": ["a", "b", "c"]})
    report_mod._enrich_reasons([cand], _Settings(), profiles={})
    assert '"cross_source_count": 3' in captured["prompt"]


def test_enrich_reasons_system_prompt_lists_anti_patterns(monkeypatch):
    captured = {}

    def fake(prompt, system, settings, temperature=None):
        captured["system"] = system
        return "[]"

    monkeypatch.setattr(report_mod, "call_stage1", fake)
    cand = _make_candidate()
    report_mod._enrich_reasons([cand], _Settings(), profiles={})
    sys_lower = captured["system"].lower()
    for banned in ["sonic", "undeniable", "journey", "vibes", "must-hear", "perfect for"]:
        assert banned in sys_lower


def test_format_weekly_stats_summarises_sections(sample_profiles):
    from src.pipeline.report import _format_weekly_stats
    sections = {
        "top_picks": [
            Candidate(artist="Sully", title="T1", link="", source="s", label="L1",
                      genre_tags=["breaks"]),
            Candidate(artist="Skee Mask", title="T2", link="", source="s", label="L1",
                      genre_tags=["breaks", "electronica"]),
        ],
        "wildcards": [
            Candidate(artist="Unknown", title="T3", link="", source="s", label="L2",
                      genre_tags=["dnb"]),
        ],
    }
    line = _format_weekly_stats(sections, sample_profiles)
    assert "3 tracks" in line
    assert "2 labels" in line
    assert "2 known artists" in line
    assert "Top genres:" in line


def test_format_weekly_stats_empty_returns_empty_string():
    from src.pipeline.report import _format_weekly_stats
    assert _format_weekly_stats({}, None) == ""
    assert _format_weekly_stats({"top_picks": []}, None) == ""


def test_format_mix_prep_stats_omits_labels_and_known_artists():
    from src.pipeline.report import _format_mix_prep_stats
    sections = {
        "top_picks": [
            Candidate(artist="A", title="T1", link="", source="s", label="L1",
                      genre_tags=["dnb"]),
            Candidate(artist="B", title="T2", link="", source="s", label="L1",
                      genre_tags=["dnb", "breaks"]),
        ],
    }
    line = _format_mix_prep_stats(sections)
    assert "2 tracks" in line
    assert "Top genres:" in line
    assert "dnb" in line
    assert "labels" not in line
    assert "known artists" not in line


def test_format_mix_prep_stats_empty():
    from src.pipeline.report import _format_mix_prep_stats
    assert _format_mix_prep_stats({}) == ""


def test_generate_report_system_prompt_includes_anti_patterns(monkeypatch, sample_profiles):
    captured = {}

    def fake_call_stage2(prompt, system, settings):
        captured["system"] = system
        captured["prompt"] = prompt
        return "Body of report"

    def fake_call_stage1(prompt, system, settings, temperature=None):
        return "[]"

    monkeypatch.setattr(report_mod, "call_stage2", fake_call_stage2)
    monkeypatch.setattr(report_mod, "call_stage1", fake_call_stage1)

    sections = {"top_picks": [_make_candidate()]}
    report_mod.generate_report(sections, "TEST", {}, _Settings(), profiles=sample_profiles)

    sys_lower = captured["system"].lower()
    assert "sonic" in sys_lower
    assert "undeniable" in sys_lower
    assert "no filler intro" in sys_lower
    assert "no closing summary" in sys_lower
    assert "This week:" in captured["prompt"]
