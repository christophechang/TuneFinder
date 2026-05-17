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
