"""
Auto-tuning (feedback loop spec, Slice B) — deterministic, bounded learning of
per-signal score multipliers from measured feedback lift.

State lives in data/learned_weights.json ONLY; deleting the file is a full
reset to baseline ScoringWeights. settings.yaml is never touched. Only the
weekly run updates the state; mix-prep and explain apply it read-only.
"""
from __future__ import annotations

import json
import os

from src.logger import get_logger
from src.pipeline.storage import atomic_write_json

logger = get_logger(__name__)

LEARNED_WEIGHTS_FILE = "learned_weights.json"
MULTIPLIER_MIN = 0.25
MULTIPLIER_MAX = 4.0
LEARNING_RATE = 0.2
MIN_SAMPLES = 10

# Codes eligible for tuning. Penalty codes (skipped_artist,
# recent_recommendation, pool_age) are excluded — lift-based tuning is
# directionally wrong for a penalty (tracks carrying one convert worse by
# construction). Feedback-derived codes (liked_artist, liked_label) and the
# zero-weight seeded tag are excluded — measuring them with the marks that
# created them is double-dipping.
TUNABLE_SIGNALS = frozenset({
    "known_artist", "recurring_artist", "label_match", "scene_adjacent",
    "cross_source", "genre_match", "fresh_release", "chart_position",
    "bandcamp_discovery", "source_popularity",
})


def load_learned_weights(data_dir: str) -> dict[str, dict]:
    path = os.path.join(data_dir, LEARNED_WEIGHTS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[learning] Corrupt {path} ignored ({exc}) — baseline weights")
        return {}
    return data if isinstance(data, dict) else {}


def save_learned_weights(learned: dict[str, dict], data_dir: str) -> None:
    path = os.path.join(data_dir, LEARNED_WEIGHTS_FILE)
    atomic_write_json(path, learned)
    logger.info(f"[learning] Saved {len(learned)} learned signal weights to {path}")


def update_learned_weights(
    learned: dict[str, dict], tune: dict, now_iso: str,
) -> tuple[dict[str, dict], list[str]]:
    """One convergent update step per signal from tune_data output.

    target = clamp(lift, MIN, MAX); new = old + LEARNING_RATE * (target - old).
    The fixed point is the (clamped) lift itself — multipliers converge, they
    don't ratchet into the clamps. Gated signals (fewer than MIN_SAMPLES rated
    marks this run, or an undefined lift) keep their prior entry untouched.
    """
    baseline = tune.get("baseline") or 0.0
    slots = tune.get("dimensions", {}).get("signal", {})
    new_learned = dict(learned)
    lines: list[str] = []

    if baseline <= 0:
        return new_learned, lines

    for code in sorted(TUNABLE_SIGNALS):
        slot = slots.get(code)
        if not slot:
            continue
        samples = slot.get("non_own", 0)
        if samples < MIN_SAMPLES:
            continue
        lift = (slot.get("positive", 0) / samples) / baseline
        target = min(max(lift, MULTIPLIER_MIN), MULTIPLIER_MAX)
        old = learned.get(code, {}).get("multiplier", 1.0)
        new = old + LEARNING_RATE * (target - old)
        new_learned[code] = {
            "multiplier": round(new, 4),
            "lift": round(lift, 4),
            "samples": samples,
            "updated_at": now_iso,
        }
        if abs(new - old) >= 0.005:
            lines.append(f"{code} ×{old:.2f}→×{new:.2f} (lift {lift:.2f}, n={samples})")
    return new_learned, lines


def signal_multipliers(learned: dict[str, dict]) -> dict[str, float]:
    """Non-neutral multipliers by signal code — what the ranker actually applies."""
    out: dict[str, float] = {}
    for code, entry in learned.items():
        m = entry.get("multiplier", 1.0)
        if abs(m - 1.0) > 1e-9:
            out[code] = m
    return out
