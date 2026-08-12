"""Tests for src/pipeline/learning.py — bounded convergent auto-tuning."""
import pytest

from src.pipeline.learning import (
    MIN_SAMPLES,
    MULTIPLIER_MAX,
    MULTIPLIER_MIN,
    TUNABLE_SIGNALS,
    load_learned_weights,
    save_learned_weights,
    signal_multipliers,
    update_learned_weights,
)


def _tune(code="label_match", positive=8, non_own=20, baseline=0.2):
    return {
        "baseline": baseline,
        "dimensions": {"signal": {code: {
            "recommended": 100, "marked": non_own, "positive": positive, "non_own": non_own,
        }}},
    }


def test_update_moves_toward_lift():
    # rate 8/20 = 0.4, baseline 0.2 → lift 2.0; new = 1.0 + 0.2*(2.0-1.0) = 1.2
    learned, lines = update_learned_weights({}, _tune(), "2026-08-12T00:00:00")
    assert learned["label_match"]["multiplier"] == pytest.approx(1.2)
    assert learned["label_match"]["lift"] == pytest.approx(2.0)
    assert learned["label_match"]["samples"] == 20
    assert learned["label_match"]["updated_at"] == "2026-08-12T00:00:00"
    assert any("label_match" in l for l in lines)


def test_update_converges_to_lift_not_clamp():
    learned = {}
    for _ in range(60):
        learned, _ = update_learned_weights(learned, _tune(positive=7, non_own=25, baseline=0.2), "t")
    # lift = (7/25)/0.2 = 1.4 → converges to 1.4, NOT to MULTIPLIER_MAX
    assert learned["label_match"]["multiplier"] == pytest.approx(1.4, abs=0.01)


def test_extreme_lift_clamped():
    learned = {}
    for _ in range(100):
        learned, _ = update_learned_weights(learned, _tune(positive=20, non_own=20, baseline=0.05), "t")
    assert learned["label_match"]["multiplier"] == pytest.approx(MULTIPLIER_MAX, abs=0.001)


def test_zero_positive_converges_to_floor():
    learned = {}
    for _ in range(100):
        learned, _ = update_learned_weights(learned, _tune(positive=0, non_own=20), "t")
    assert learned["label_match"]["multiplier"] == pytest.approx(MULTIPLIER_MIN, abs=0.001)


def test_gate_below_min_samples_untouched():
    learned, lines = update_learned_weights({}, _tune(non_own=MIN_SAMPLES - 1, positive=4), "t")
    assert "label_match" not in learned
    assert lines == []


def test_zero_baseline_no_update():
    learned, _ = update_learned_weights({}, _tune(baseline=0.0), "t")
    assert learned == {}


def test_non_allowlisted_codes_never_tuned():
    tune = _tune(code="skipped_artist")
    learned, _ = update_learned_weights({}, tune, "t")
    assert learned == {}
    for code in ("skipped_artist", "recent_recommendation", "pool_age",
                 "liked_artist", "liked_label", "seeded"):
        assert code not in TUNABLE_SIGNALS


def test_existing_entry_kept_when_gated_this_run():
    prior = {"label_match": {"multiplier": 1.3, "lift": 1.5, "samples": 30, "updated_at": "old"}}
    learned, _ = update_learned_weights(prior, _tune(non_own=3, positive=1), "t")
    assert learned["label_match"]["multiplier"] == 1.3  # preserved, not reset


def test_no_adjustment_line_for_tiny_moves():
    # already converged: multiplier == lift → move is 0
    prior = {"label_match": {"multiplier": 2.0, "lift": 2.0, "samples": 20, "updated_at": "old"}}
    learned, lines = update_learned_weights(prior, _tune(), "t")
    assert lines == []
    assert learned["label_match"]["multiplier"] == pytest.approx(2.0)


def test_round_trip_persistence(tmp_path):
    learned, _ = update_learned_weights({}, _tune(), "2026-08-12T00:00:00")
    save_learned_weights(learned, str(tmp_path))
    assert load_learned_weights(str(tmp_path)) == learned


def test_load_missing_or_corrupt_returns_empty(tmp_path):
    assert load_learned_weights(str(tmp_path)) == {}
    (tmp_path / "learned_weights.json").write_text("{broken")
    assert load_learned_weights(str(tmp_path)) == {}


def test_signal_multipliers_skips_neutral():
    learned = {"a": {"multiplier": 1.0}, "b": {"multiplier": 1.25}}
    assert signal_multipliers(learned) == {"b": 1.25}


def test_load_drops_malformed_entries(tmp_path):
    import json
    (tmp_path / "learned_weights.json").write_text(json.dumps({
        "label_match": {"multiplier": 1.2, "lift": 1.4, "samples": 20, "updated_at": "t"},
        "broken_str": "nope",
        "broken_mult": {"multiplier": "high"},
    }))
    learned = load_learned_weights(str(tmp_path))
    assert list(learned) == ["label_match"]
