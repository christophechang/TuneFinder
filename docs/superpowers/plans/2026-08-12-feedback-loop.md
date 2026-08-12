# Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the feedback loop — positive marks boost future ranking, own/bought tracks stop resurfacing, scoring weights auto-tune from measured lift, and liked artists seed fetch queries.

**Architecture:** All learning is deterministic and derived at run time from `data/feedback.json` joined to recommendation history. The only new persistent state is `data/learned_weights.json` (delete = full reset). Scoring changes ride the existing signal machinery (`RecommendationSignal`, explain trace, report artifact). Web surfaces the learned state via a new `GET /api/learning` endpoint.

**Tech Stack:** Python 3.11 (engine, pytest), FastAPI (web API), Vite + React 18 + TypeScript strict + vitest (SPA).

**Spec:** `docs/superpowers/specs/2026-08-12-feedback-loop-design.md`

## Global Constraints

- Learned state never touches `config/settings.yaml`; it lives in `data/learned_weights.json` only.
- `--dry-run` never writes `learned_weights.json` (nor any other store — existing rule).
- Update rule: `target = clamp(lift, 0.25, 4.0)`; `multiplier = old + 0.2 × (target − old)`. Gate: ≥ 10 non-neutral marks carrying the signal; null/zero-baseline lift → no update.
- Tunable allowlist (exactly): `known_artist`, `recurring_artist`, `label_match`, `scene_adjacent`, `cross_source`, `genre_match`, `fresh_release`, `chart_position`, `bandcamp_discovery`, `source_popularity`. Penalty codes and `liked_artist`/`liked_label`/`seeded` are NEVER tuned.
- Multipliers scale a signal's **final contribution** (after internal caps), applied identically in weekly, mix-prep, and explain paths.
- Positive-artist boost requires zero latest-mark `skip` for that artist (mirror of `skipped_artists`' zero-positive rule).
- `bought` strength 2.0, `liked` 1.0; per-artist/per-label strength cap 6.0.
- `heard` stays excluded from lift rates (existing `NEUTRAL_OUTCOMES` convention) — do not change lift math.
- Only the **weekly** run updates learned weights; mix-prep applies them read-only.
- All keyword args added to `_score`/`rank_candidates*` default to `None` so every existing test keeps passing unchanged.
- Engine work on branch `feat/feedback-loop` off `develop`; conventional commits; slice-labeled commit groups A → B → C. Web work on its own branch off `develop` in tunefinder-web.
- After each task: `./venv/bin/python -m pytest tests/ -q` must pass (engine) / `npx vitest run` (web).

---

## Slice A — positive signals + identity fix

### Task A1: Feedback derivations — `positive_artists`, `positive_labels`, `feedback_known_keys`

**Files:**
- Modify: `src/pipeline/feedback.py` (after `skipped_artists`, ~line 240)
- Test: `tests/test_feedback.py`

**Interfaces:**
- Produces: `positive_artists(entries: list[FeedbackEntry]) -> dict[str, float]` (normalised artist → strength), `positive_labels(entries, weekly: list[RecommendationRecord], mix_prep: list[RecommendationRecord]) -> dict[str, float]` (lowercased label → strength), `feedback_known_keys(entries, remix_aware: bool = False) -> set[str]`, module constants `POSITIVE_STRENGTH_CAP = 6.0`, `_POSITIVE_STRENGTHS = {"bought": 2.0, "liked": 1.0}`.

- [ ] **Step 1: Write failing tests** in `tests/test_feedback.py` (follow the existing `_entry(...)` helper style already used by the `skipped_artists` tests there; add the needed top-level imports — `normalise_artist`, `make_dedup_key`, `RecommendationRecord`, and the three new functions — `make_dedup_key` is currently only imported inside `_entry`):

```python
def test_positive_artists_bought_outweighs_liked():
    entries = [
        _entry("Om Unit", "Track A", "bought"),
        _entry("Sully", "Track B", "liked"),
    ]
    strengths = positive_artists(entries)
    assert strengths[normalise_artist("Om Unit")] == 2.0
    assert strengths[normalise_artist("Sully")] == 1.0

def test_positive_artists_any_skip_disqualifies():
    entries = [
        _entry("Sully", "Track B", "liked"),
        _entry("Sully", "Track C", "skip"),
    ]
    assert positive_artists(entries) == {}

def test_positive_artists_strength_capped():
    entries = [_entry("Om Unit", f"T{i}", "bought") for i in range(5)]
    assert positive_artists(entries)[normalise_artist("Om Unit")] == 6.0

def test_positive_artists_latest_mark_wins():
    # liked then re-marked skip on the same track → no boost.
    # NOTE: the existing _entry helper takes days_ago (it computes marked_at
    # itself) — same convention as the re-mark tests around line 281.
    entries = [
        _entry("Sully", "Track B", "liked", days_ago=31),
        _entry("Sully", "Track B", "skip", days_ago=0),
    ]
    assert positive_artists(entries) == {}

def test_positive_artists_splits_collaborations():
    entries = [_entry("Bakey, Kasia", "Track A", "liked")]
    strengths = positive_artists(entries)
    assert strengths[normalise_artist("Bakey")] == 1.0
    assert strengths[normalise_artist("Kasia")] == 1.0

def test_positive_labels_joins_history_label():
    rec = RecommendationRecord(artist="Sully", title="Track B", link="", source="beatport",
                               recommended_at="2026-01-01", report_id="2026-W01",
                               label="Astrophonica")
    entries = [_entry("Sully", "Track B", "bought")]
    strengths = positive_labels(entries, [rec], [])
    assert strengths["astrophonica"] == 2.0

def test_positive_labels_no_record_or_label_is_skipped():
    assert positive_labels([_entry("X", "Y", "liked")], [], []) == {}

def test_feedback_known_keys_own_and_bought_only():
    entries = [
        _entry("A", "T1", "own"),
        _entry("B", "T2", "bought"),
        _entry("C", "T3", "liked"),
        _entry("D", "T4", "skip"),
    ]
    keys = feedback_known_keys(entries)
    assert make_dedup_key("A", "T1") in keys
    assert make_dedup_key("B", "T2") in keys
    assert make_dedup_key("C", "T3") not in keys
    assert make_dedup_key("D", "T4") not in keys

def test_feedback_known_keys_remix_aware_emits_both_regimes():
    entries = [_entry("A", "T1 (Sully Remix)", "own")]
    keys = feedback_known_keys(entries, remix_aware=True)
    assert make_dedup_key("A", "T1 (Sully Remix)") in keys
    assert make_dedup_key("A", "T1 (Sully Remix)", remix_aware=True) in keys
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/python -m pytest tests/test_feedback.py -q` → NameError / ImportError.

- [ ] **Step 3: Implement** in `src/pipeline/feedback.py`:

```python
# --- Positive feedback derivations (feedback loop spec, Slice A) ---

_POSITIVE_STRENGTHS = {"bought": 2.0, "liked": 1.0}
POSITIVE_STRENGTH_CAP = 6.0

def positive_artists(entries: list[FeedbackEntry]) -> dict[str, float]:
    """Normalised artist → positive strength (bought=2, liked=1, capped).

    Mirror image of skipped_artists: any latest-mark 'skip' on the artist
    disqualifies the boost entirely, so one mark can never both cancel the
    skip penalty and add a boost. Neutral outcomes are no-ops.
    """
    strengths: dict[str, float] = {}
    has_skip: set[str] = set()
    for entry in latest_marks(entries):
        for part in _split_artists(entry.artist):
            name = normalise_artist(part)
            if not name:
                continue
            if entry.outcome == "skip":
                has_skip.add(name)
            elif entry.outcome in _POSITIVE_STRENGTHS:
                strengths[name] = strengths.get(name, 0.0) + _POSITIVE_STRENGTHS[entry.outcome]
    return {
        name: min(s, POSITIVE_STRENGTH_CAP)
        for name, s in strengths.items() if name not in has_skip
    }


def positive_labels(
    entries: list[FeedbackEntry],
    weekly: list[RecommendationRecord],
    mix_prep: list[RecommendationRecord],
) -> dict[str, float]:
    """Lowercased label → positive strength, joined via recommendation records
    (same (history, key) join as tune_data; ranker's lower().strip() convention).
    """
    records_by_hk: dict[tuple[str, str], RecommendationRecord] = {}
    for history_name, records in (("weekly", weekly), ("mix-prep", mix_prep)):
        for r in records:
            hk = (history_name, make_dedup_key(r.artist, r.title))
            if hk not in records_by_hk or r.recommended_at > records_by_hk[hk].recommended_at:
                records_by_hk[hk] = r

    strengths: dict[str, float] = {}
    for entry in latest_marks(entries):
        if entry.outcome not in _POSITIVE_STRENGTHS:
            continue
        rec = records_by_hk.get((entry.history, entry.key))
        if rec is None or not rec.label:
            continue
        label_key = rec.label.lower().strip()
        strengths[label_key] = strengths.get(label_key, 0.0) + _POSITIVE_STRENGTHS[entry.outcome]
    return {k: min(s, POSITIVE_STRENGTH_CAP) for k, s in strengths.items()}


def feedback_known_keys(entries: list[FeedbackEntry], remix_aware: bool = False) -> set[str]:
    """Exclusion keys for tracks whose latest mark is 'own' or 'bought'.

    Emits both key regimes when remix_aware (mirror of build_known_track_keys)
    so the merge behaves under either pipeline.remix_aware_identity setting.
    """
    keys: set[str] = set()
    for entry in latest_marks(entries):
        if entry.outcome in ("own", "bought"):
            keys.add(make_dedup_key(entry.artist, entry.title))
            if remix_aware:
                keys.add(make_dedup_key(entry.artist, entry.title, remix_aware=True))
    return keys
```

- [ ] **Step 4: Run tests** — `./venv/bin/python -m pytest tests/test_feedback.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(feedback): positive artist/label strengths and own/bought exclusion keys"`

### Task A2: Ranker signals `liked_artist` + `liked_label`

**Files:**
- Modify: `src/pipeline/ranker.py` (`ScoringWeights`, `_score`, `rank_candidates`, `rank_candidates_mix_prep`)
- Test: `tests/test_ranker.py`

**Interfaces:**
- Consumes: `positive_artists` / `positive_labels` outputs (dicts from A1).
- Produces: `ScoringWeights` fields `w_liked_artist: float = 0.75`, `liked_artist_cap: float = 3.0`, `w_liked_label: float = 0.5`. New keyword params (default `None`) threaded through: `_score(..., positive_artist_strengths=None, positive_label_strengths=None)`, same on `rank_candidates` and `rank_candidates_mix_prep`.

- [ ] **Step 1: Write failing tests** in `tests/test_ranker.py` (follow the file's existing `_score`-direct test style with empty profiles; add `from src.pipeline.dedup import normalise_artist` — the file doesn't import it today):

```python
def test_liked_artist_signal_fires_and_caps():
    c = Candidate(artist="Sully", title="New One", link="", source="beatport")
    _score(c, {}, set(), {}, set(),
           positive_artist_strengths={normalise_artist("Sully"): 6.0})
    assert any(s.code == "liked_artist" for s in c.signals)
    # 0.75 * 6.0 = 4.5 → capped at liked_artist_cap 3.0
    assert c.score == 3.0
    assert c.familiarity_score == 3.0

def test_liked_artist_absent_without_strengths():
    c = Candidate(artist="Sully", title="New One", link="", source="beatport")
    _score(c, {}, set(), {}, set())
    assert not any(s.code == "liked_artist" for s in c.signals)

def test_liked_label_signal_flat_boost():
    c = Candidate(artist="Unknown", title="T", link="", source="beatport", label="Astrophonica")
    _score(c, {}, set(), {}, set(),
           positive_label_strengths={"astrophonica": 2.0})
    assert any(s.code == "liked_label" for s in c.signals)
    assert c.score == 0.5
    assert c.discovery_score == 0.5
```

- [ ] **Step 2: Run to verify failure** — `./venv/bin/python -m pytest tests/test_ranker.py -q` → TypeError (unexpected kwarg).

- [ ] **Step 3: Implement.** Add to `ScoringWeights` (after the skip-derived block, ~line 75):

```python
    # --- Positive feedback signals (feedback loop spec, Slice A) ---
    w_liked_artist: float = 0.75     # × positive strength (bought=2/liked=1, capped), familiarity axis
    liked_artist_cap: float = 3.0    # cap on the liked_artist contribution
    w_liked_label: float = 0.5       # flat boost when the label carries positive marks, discovery axis
```

Add params to `_score` signature: `positive_artist_strengths: dict[str, float] | None = None, positive_label_strengths: dict[str, float] | None = None`. Insert after the skip-penalty block (~line 388):

```python
    # --- Positive feedback signals (feedback loop spec, Slice A) ---
    # liked_artist mirrors skipped_artist in reverse: derived from latest marks
    # (feedback.positive_artists), mutually exclusive with the skip penalty at
    # the derivation level. Familiarity axis — you have direct evidence you
    # like this artist. Deliberately NOT auto-tuned (Slice B allowlist).
    if positive_artist_strengths:
        liked_name = None
        liked_strength = 0.0
        for part in artist_parts:
            s = positive_artist_strengths.get(normalise_artist(part), 0.0)
            if s > liked_strength:
                liked_strength = s
                liked_name = part.strip()
        if liked_name is not None:
            bonus = min(weights.w_liked_artist * liked_strength, weights.liked_artist_cap)
            score += bonus
            familiarity += bonus
            c.signals.append(RecommendationSignal(
                code="liked_artist",
                explanation=f"You've liked or bought {liked_name} from past reports.",
            ))

    # liked_label — flat nudge when the label itself carries positive marks.
    if positive_label_strengths and c.label:
        if c.label.lower().strip() in positive_label_strengths:
            score += weights.w_liked_label
            discovery += weights.w_liked_label
            c.signals.append(RecommendationSignal(
                code="liked_label",
                explanation=f"{c.label} — you've liked or bought tracks on this label.",
            ))
```

Thread both params through `rank_candidates` and `rank_candidates_mix_prep` (signature + the `_score(...)` call, same pattern as `skip_penalty_artists`).

- [ ] **Step 4: Run tests** — `./venv/bin/python -m pytest tests/test_ranker.py -q` → PASS (all existing tests too — defaults are None).
- [ ] **Step 5: Commit** — `git commit -m "feat(ranker): liked_artist and liked_label positive signals"`

### Task A3: Known-track merge + pool-injection normalised check

**Files:**
- Modify: `src/services/runs.py` (`_load_profile_state`, pool injection in `run_weekly` ~line 250 and `run_mix_prep` ~line 520), `src/pipeline/explain.py` (~line 67 and ~line 188)
- Test: `tests/test_services_runs.py`, `tests/test_explain.py`

**Interfaces:**
- Consumes: `feedback_known_keys` (A1).
- Produces: `_load_profile_state` returns `known_keys` already unioned with feedback-derived keys — every consumer (weekly, mix-prep, explain, pool injection) inherits the merge.

- [ ] **Step 1: Write failing test** in `tests/test_services_runs.py` (reuse the file's existing patched-fetcher run harness — find the existing weekly-run test that patches `fetch_all_tracks`/`fetch_all_sources` and copy its setup):

```python
def test_owned_feedback_track_never_resurfaces(tmp_path, ...):
    # Arrange: feedback.json contains latest mark own for "Artist X - Track Y";
    # sources return that exact track. Run weekly (dry_run).
    # Assert: track absent from every section of the artifact.

def test_bought_pool_record_not_injected(tmp_path, ...):
    # Arrange: candidate_pool.json holds "Artist X - Track Y (Original Mix)";
    # feedback latest mark bought with key make_dedup_key("Artist X", "Track Y").
    # Pool record's raw .key ("artist x||track y (original mix)") does NOT match,
    # but the normalised make_dedup_key does. Run weekly (dry_run).
    # Assert: pool_injected == 0 in outcome.stats.
```

Also in `tests/test_explain.py`: an `own`-marked track shows `FILTERED` in the KNOWN-TRACK FILTER section.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** In `_load_profile_state`, merge at both return points:

```python
    from src.pipeline.feedback import load_feedback, feedback_known_keys
    fb_keys = feedback_known_keys(load_feedback(settings.data_dir), remix_aware)
```

— in the exception path: `return profiles, genre_affinity, known_keys | fb_keys, True`; in the success path: `return profiles, genre_affinity, known_keys | fb_keys, False` (compute `fb_keys` once near the top; the docstring gains a line noting the merge). In `run_weekly` pool injection, replace the condition with:

```python
        pool_injected = [
            c for c in pool_to_candidates([r for r in pool_records if r.key not in fresh_keys])
            if c.key not in known_keys
            and make_dedup_key(c.artist, c.title, remix_aware) not in known_keys
            and c.key not in history_keys
        ]
```

(add `make_dedup_key` to the function-local dedup import). Same change in `run_mix_prep`'s pool injection and in `explain.py`'s two known-key sites: `known_keys = load_known_tracks(settings.data_dir) | feedback_known_keys(feedback_entries, remix_aware)` (move the `load_feedback` call above it) and its pool-injection comprehension gains the same normalised check.

- [ ] **Step 4: Run tests** — `./venv/bin/python -m pytest tests/test_services_runs.py tests/test_explain.py tests/test_degraded_profile.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(pipeline): merge own/bought marks into known-track exclusion"`

### Task A4: Wire positive signals into runs + explain

**Files:**
- Modify: `src/services/runs.py` (`run_weekly` step 5, `run_mix_prep` step 5), `src/pipeline/explain.py` (scoring context)
- Test: `tests/test_services_runs.py`, `tests/test_explain.py`

**Interfaces:**
- Consumes: A1 derivations, A2 ranker params.

- [ ] **Step 1: Write failing test** — weekly run harness: feedback contains `liked` for "Artist Z"; sources return a new track by Artist Z; assert the artifact track carries a `liked_artist` signal code. Explain test: same setup, trace contains `[liked_artist]`.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** In `run_weekly` step 5 (after `skip_set`):

```python
        from src.pipeline.history import load_mix_prep_history
        positive_strengths = positive_artists(feedback_entries)
        mix_prep_history = load_mix_prep_history(settings.data_dir)
        label_strengths = positive_labels(feedback_entries, history, mix_prep_history)
        sections, label_artists = rank_candidates(
            candidates, profiles, settings, label_seed=label_seed, genre_affinity=genre_affinity,
            label_memory=label_memory, skip_penalty_artists=skip_set,
            positive_artist_strengths=positive_strengths,
            positive_label_strengths=label_strengths,
        )
```

(update the function-local `from src.pipeline.feedback import ...` to include the new names). Mirror in `run_mix_prep` (it must additionally `load_history` for the weekly side of the label join). In `explain.py`, build the same two dicts (it already loads feedback + history; add `load_mix_prep_history`) and pass them to both `_score` calls.

- [ ] **Step 4: Run full suite** — `./venv/bin/python -m pytest tests/ -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(runs): apply positive feedback signals in weekly, mix-prep and explain"`

---

## Slice B — auto-tuning

### Task B1: `learning.py` — lift, update rule, persistence

**Files:**
- Create: `src/pipeline/learning.py`
- Test: `tests/test_learning.py` (new)

**Interfaces:**
- Consumes: `tune_data(...)` output shape (`{"baseline": float, "dimensions": {"signal": {code: {recommended, marked, positive, non_own}}}}`).
- Produces:
  - `TUNABLE_SIGNALS: frozenset[str]`, `MIN_SAMPLES = 10`, `LEARNING_RATE = 0.2`, `MULTIPLIER_MIN = 0.25`, `MULTIPLIER_MAX = 4.0`, `LEARNED_WEIGHTS_FILE = "learned_weights.json"`.
  - `load_learned_weights(data_dir: str) -> dict[str, dict]` (missing/corrupt file → `{}`)
  - `save_learned_weights(learned: dict[str, dict], data_dir: str) -> None` (atomic_write_json)
  - `update_learned_weights(learned: dict[str, dict], tune: dict, now_iso: str) -> tuple[dict[str, dict], list[str]]` — returns (new state, human-readable adjustment lines like `label_match ×1.08 (lift 1.40, n=23)`)
  - `signal_multipliers(learned: dict[str, dict]) -> dict[str, float]` — `{code: multiplier}` for codes with multiplier ≠ 1.0 (within 1e-9), else omitted.

- [ ] **Step 1: Write failing tests** in `tests/test_learning.py`:

```python
import pytest

from src.pipeline.learning import (
    MIN_SAMPLES, MULTIPLIER_MAX, MULTIPLIER_MIN, TUNABLE_SIGNALS,
    load_learned_weights, save_learned_weights, signal_multipliers,
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
    assert learned["label_match"]["multiplier"] == pytest.approx(MULTIPLIER_MAX)

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
    for code in ("skipped_artist", "recent_recommendation", "pool_age", "liked_artist", "liked_label", "seeded"):
        assert code not in TUNABLE_SIGNALS

def test_existing_entry_kept_when_gated_this_run():
    prior = {"label_match": {"multiplier": 1.3, "lift": 1.5, "samples": 30, "updated_at": "old"}}
    learned, _ = update_learned_weights(prior, _tune(non_own=3, positive=1), "t")
    assert learned["label_match"]["multiplier"] == 1.3  # preserved, not reset

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
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Implement** `src/pipeline/learning.py`:

```python
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
    Fixed point is the (clamped) lift itself — multipliers converge, they
    don't ratchet into the clamps. Gated signals keep their prior entry.
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
    out: dict[str, float] = {}
    for code, entry in learned.items():
        m = entry.get("multiplier", 1.0)
        if abs(m - 1.0) > 1e-9:
            out[code] = m
    return out
```

- [ ] **Step 4: Run tests** — `./venv/bin/python -m pytest tests/test_learning.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(learning): convergent bounded per-signal multiplier learning"`

### Task B2: Ranker applies multipliers to signal contributions

**Files:**
- Modify: `src/pipeline/ranker.py` (`_score`, `rank_candidates`, `rank_candidates_mix_prep`)
- Test: `tests/test_ranker.py`

**Interfaces:**
- Produces: `_score(..., signal_multipliers: dict[str, float] | None = None)`; same param on both `rank_candidates*`.

- [ ] **Step 1: Write failing tests:**

```python
def test_multiplier_scales_signal_contribution():
    profiles = {"sully": ArtistProfile(name="Sully", play_count=2)}
    c = Candidate(artist="Sully", title="T", link="", source="beatport")
    _score(c, profiles, set(), {}, set(), signal_multipliers={"known_artist": 1.5})
    # base known_artist: 2 plays * 3.0 = 6.0 → ×1.5 = 9.0 (still under 10.0 cap)
    assert c.score == 9.0
    assert c.familiarity_score == 9.0

def test_multiplier_applies_after_cap():
    profiles = {"sully": ArtistProfile(name="Sully", play_count=10)}
    c = Candidate(artist="Sully", title="T", link="", source="beatport")
    _score(c, profiles, set(), {}, set(), signal_multipliers={"known_artist": 0.5})
    # known_artist: 10 plays * 3.0 = 30 → capped 10.0 → ×0.5 = 5.0.
    # play_count 10 also clears recurring_threshold 3 → recurring_artist +2.0
    # (untouched — its own multiplier wasn't passed). Total 7.0.
    assert c.score == 7.0
    assert c.familiarity_score == 7.0

def test_multiplier_ignores_unknown_and_penalty_codes():
    c = Candidate(artist="Nobody", title="T", link="", source="bandcamp")
    _score(c, {}, set(), {}, set(),
           signal_multipliers={"pool_age": 4.0, "bandcamp_discovery": 2.0})
    # bandcamp_discovery 1.0 ×2.0 = 2.0; pool_age multiplier has no block to land in
    assert c.score == 2.0
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** In `_score`, add the param and a local helper near the top:

```python
    mult = signal_multipliers or {}

    def _m(code: str) -> float:
        return mult.get(code, 1.0)
```

Then scale each tunable signal's **final contribution** — the multiplier lands AFTER internal formulas and caps so learning is symmetric in both directions (a >1 multiplier on a capped artist would otherwise be a no-op while <1 always bites):

- `known_artist`: `artist_score = min(artist_score, weights.max_artist_score) * _m("known_artist")`
- `recurring_artist`: `bonus = weights.w_recurring * _m("recurring_artist")` (use `bonus` for both `score`/`familiarity`)
- `label_match`: `label_bonus = (weights.w_label_base + weights.w_label_per_artist * known_on_label) * _m("label_match")`
- `scene_adjacent`: `bonus = weights.w_scene_adjacent * _m("scene_adjacent")`
- `cross_source`: `cross_source_bonus = weights.w_cross_source_per * capped * _m("cross_source")`
- `genre_match`: `genre_bonus = (...existing sum...) * _m("genre_match")`
- `fresh_release`: `bonus = weights.w_fresh * _m("fresh_release")`
- `chart_position`: `chart_bonus = weights.w_chart_top * (1 - (chart_pos - 1) / _CHART_SCALE) * _m("chart_position")`
- `bandcamp_discovery`: `bonus = weights.w_bandcamp * _m("bandcamp_discovery")`
- `source_popularity` (both mixupload and soundcloud blocks): `bonus = weights.w_*_popularity * _m("source_popularity")`

Penalty blocks (`recent_recommendation`, `skipped_artist`, `pool_age`) and the Slice-A blocks (`liked_artist`, `liked_label`) do NOT consult `_m` — defense in depth on top of the allowlist. Thread `signal_multipliers` through both `rank_candidates*` signatures into `_score`.

- [ ] **Step 4: Run tests** — `./venv/bin/python -m pytest tests/test_ranker.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(ranker): apply learned multipliers to tunable signal contributions"`

### Task B3: Wire learning into runs + explain + Discord summary

**Files:**
- Modify: `src/services/runs.py` (both runs), `src/pipeline/explain.py`
- Test: `tests/test_services_runs.py`, `tests/test_explain.py`

**Interfaces:**
- Consumes: B1 module, B2 ranker param.
- Produces: weekly run updates + saves learned weights (live only) then applies them; mix-prep applies read-only; explain applies read-only and prints active multipliers.

- [ ] **Step 1: Write failing tests:**

```python
def test_weekly_run_updates_learned_weights(tmp_path, ...):
    # Arrange feedback + history so label_match clears MIN_SAMPLES with lift > 1.
    # Live run (dry_run=False, discord patched). Assert learned_weights.json
    # exists and label_match multiplier > 1.0.

def test_dry_run_never_writes_learned_weights(tmp_path, ...):
    # Same arrangement, dry_run=True → data/learned_weights.json absent.

def test_mix_prep_applies_but_never_updates(tmp_path, ...):
    # Pre-write learned_weights.json {label_match: 2.0}; run mix-prep live;
    # assert file content unchanged (mtime/content) after run.
```

Explain test: pre-write `learned_weights.json` with `{"label_match": {"multiplier": 1.5, ...}}`; trace contains `Learned multipliers` and `label_match ×1.50`.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** `run_weekly` step 5 — after `skip_set`/positives (A4), move `now_iso = datetime.now(timezone.utc).isoformat()` up from step 5b to here, then:

```python
        # Auto-tuning (Slice B) — one convergent update per weekly run, from
        # all marks to date, then applied to this run's scoring. Dry runs
        # compute and apply but never persist.
        from src.pipeline.feedback import tune_data
        from src.pipeline.learning import (
            load_learned_weights, save_learned_weights, signal_multipliers,
            update_learned_weights,
        )
        learned = load_learned_weights(settings.data_dir)
        tune = tune_data(history, mix_prep_history, feedback_entries)
        learned, adjustments = update_learned_weights(learned, tune, now_iso)
        if not dry_run:
            save_learned_weights(learned, settings.data_dir)
        multipliers = signal_multipliers(learned)
        for line in adjustments:
            logger.info(f"[learning] {line}")
```

Pass `signal_multipliers=multipliers` into `rank_candidates(...)`. Append to the step-9 `log_msg`:

```python
        learning_note = ("Learning: " + "; ".join(adjustments)) if adjustments else "Learning: no adjustments"
        log_msg = log_msg + f"\n{learning_note}"
```

`run_mix_prep`: `multipliers = signal_multipliers(load_learned_weights(settings.data_dir))` (no update, no save), passed to `rank_candidates_mix_prep`. `explain.py`: load + build multipliers, pass to both `_score` calls, and print after the `Dedup key` header block (explain.py:62-63; existing test assertions are substring-based, inserted lines are safe):

```python
    from src.pipeline.learning import load_learned_weights, signal_multipliers
    learned_mults = signal_multipliers(load_learned_weights(settings.data_dir))
    if learned_mults:
        lines.append("Learned multipliers (data/learned_weights.json):")
        for code, m in sorted(learned_mults.items()):
            lines.append(f"  {code} ×{m:.2f}")
    else:
        lines.append("Learned multipliers: none (baseline weights)")
    lines.append("")
```

(place after the `Dedup key` header block).

- [ ] **Step 4: Run full suite** — `./venv/bin/python -m pytest tests/ -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(runs): weekly auto-tuning of signal weights with Discord summary"`

---

## Slice C — feedback-seeded fetching

### Task C1: Seed derivation + fetcher interface threading

**Files:**
- Modify: `src/fetchers/__init__.py` (`fetch_all_sources`), every fetcher module's `fetch` signature (`beatport.py`, `bandcamp.py`, `traxsource.py`, `boomkat.py`, `bleep.py`, `ra.py`, `mixupload.py`, `volumo.py`, `soundcloud.py`), `src/config.py` (two pipeline knobs), `src/services/runs.py` (weekly only)
- Test: `tests/test_fetch_all_sources.py`, `tests/test_services_runs.py`

**Interfaces:**
- Produces: `fetch_all_sources(settings, target_genre=None, only_sources=None, bpm_ranges=None, seed_queries: list[str] | None = None)`; every fetcher gains `seed_queries: list[str] | None = None` (8 of 9 ignore it); `Settings.pipeline_seeded_artist_count` (default 10), `Settings.pipeline_seeded_label_count` (default 5).

- [ ] **Step 1: Write failing tests:**

```python
# tests/test_fetch_all_sources.py
def test_seed_queries_forwarded_to_fetchers(monkeypatch, ...):
    # Patch a fetcher, call fetch_all_sources(..., seed_queries=["om unit"]),
    # assert the fetcher received seed_queries=["om unit"].
```

```python
# tests/test_services_runs.py
def test_weekly_run_passes_seeds_from_positive_marks(tmp_path, ...):
    # feedback: 1 bought for "Om Unit"; patch fetch_all_sources, run weekly,
    # assert it was called with seed_queries containing normalise_artist("Om Unit").
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.**
  - `config.py` pipeline block:

```python
    @property
    def pipeline_seeded_artist_count(self) -> int:
        return self._data.get("pipeline", {}).get("seeded_artist_count", 10)

    @property
    def pipeline_seeded_label_count(self) -> int:
        return self._data.get("pipeline", {}).get("seeded_label_count", 5)
```

  - `fetch_all_sources`: add the param, pass `seed_queries=seed_queries` in the `fetch_fn(...)` call. Add `seed_queries: list[str] | None = None` to all nine fetchers' `fetch` signatures (docstring one-liner: "seed_queries: free-text taste-seeded queries — sources without seeded search ignore it").
  - `run_weekly`: move `feedback_entries = load_feedback(settings.data_dir)` and history loads to BEFORE step 3 (fetch), compute:

```python
        positive_strengths = positive_artists(feedback_entries)
        seed_queries = [
            name for name, _ in sorted(positive_strengths.items(), key=lambda kv: -kv[1])
        ][: settings.pipeline_seeded_artist_count]
        label_strengths = positive_labels(feedback_entries, history, mix_prep_history)
        seed_queries += [
            label for label, _ in sorted(label_strengths.items(), key=lambda kv: -kv[1])
        ][: settings.pipeline_seeded_label_count]
```

    and pass `seed_queries=seed_queries or None` to `fetch_all_sources`. Explicitly: this moves `feedback_entries = load_feedback(...)`, `skip_set = skipped_artists(...)` AND adds `mix_prep_history = load_mix_prep_history(settings.data_dir)` all to before step 3 (weekly `history` already loads pre-fetch at step 2); step 5 then reuses `positive_strengths`/`label_strengths`/`skip_set` — delete the duplicate derivations from A4. Mix-prep does NOT seed (genre-targeted runs stay genre-pure).

- [ ] **Step 4: Run tests** — full suite → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(fetchers): thread taste-seeded queries from positive marks (weekly)"`

### Task C2: SoundCloud seeded search + `seeded` zero-weight signal

**Files:**
- Modify: `src/fetchers/soundcloud.py`, `src/pipeline/ranker.py` (`_score`), `src/pipeline/report_artifact.py` (`_track_payload`)
- Test: `tests/test_soundcloud.py`, `tests/test_ranker.py`, `tests/test_report_artifact.py`

**Interfaces:**
- Produces: seeded SoundCloud items carry `raw_metadata["seeded_by"] = <seed string>` and empty `genre_tags`; `_score` appends a zero-contribution `seeded` signal when `c.raw_metadata.get("seeded_by")` is set (rides the existing by-signal lift table for free — never tuned, never scored); `_track_payload` passes `seeded_by` through to the artifact.

- [ ] **Step 1: Write failing tests:**

```python
# tests/test_soundcloud.py — follow the file's existing requests-mock style
def test_seeded_queries_searched_and_tagged(...):
    # fetch(settings, seed_queries=["om unit"]) issues a q="om unit" search
    # (in addition to configured targets) and returned items carry
    # raw_metadata["seeded_by"] == "om unit" and genre_tags == [].

def test_seeded_respects_downloadable_only(...):
    # non-downloadable, non-gated seeded result is dropped (same lane rules).
```

```python
# tests/test_ranker.py
def test_seeded_signal_zero_contribution():
    c = Candidate(artist="X", title="T", link="", source="soundcloud",
                  raw_metadata={"seeded_by": "om unit"})
    _score(c, {}, set(), {}, set())
    assert any(s.code == "seeded" for s in c.signals)
    assert c.score == 0.0
```

```python
# tests/test_report_artifact.py
def test_track_payload_carries_seeded_by():
    # candidate with raw_metadata["seeded_by"] → payload["seeded_by"] == "om unit";
    # without it → key absent or None (match existing optional-field style).
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.**
  - `soundcloud.py`: change `_parse_track(track, tag, free_gate=False)` so `tag=None` yields `genre_tags=[]` (`genre_tags=[tag] if tag else []`). In `fetch`, after the configured-targets loop (before the fail-safe check), when `seed_queries` and `target_genre is None`:

```python
    seeded_seen_ids: set = set()
    for seed in (seed_queries or []):
        polite_sleep(1.0)
        try:
            url = _build_search_url({"q": seed}, created_from, limit)
            page = 0
            while url and page < _MAX_PAGES:
                data = _get_json(url, session)
                page += 1
                for track in (data.get("collection") or []):
                    track_id = track.get("id")
                    if track_id is not None and track_id in seeded_seen_ids:
                        continue
                    gate = include_gated and _is_free_gate(track)
                    if downloadable_only and track.get("downloadable") is not True and not gate:
                        continue
                    duration = track.get("duration")
                    if max_duration_ms and duration and duration > max_duration_ms:
                        continue
                    item = _parse_track(track, None, free_gate=gate)
                    if item is None:
                        continue
                    if item.release_date is not None and item.release_date < created_from:
                        continue
                    if track_id is not None:
                        seeded_seen_ids.add(track_id)
                    item.raw_metadata["seeded_by"] = seed
                    all_items.append(item)
                url = data.get("next_href")
        except Exception as e:
            logger.warning(f"[soundcloud] seeded '{seed}': fetch failed: {e}")
```

    Placement + guards, all deliberate:
    - The early return `if not targets: return []` (soundcloud.py:249-250) must become `if not targets and not seed_queries: return []`, and the target loop must tolerate an empty `targets` list — otherwise seeding is dead whenever no static targets are configured.
    - Seeded failures never count toward the all-targets-failed fail-safe (`attempted`/`completed` untouched). If every static target fails, the existing `RuntimeError` still fires and discards seeded items too — acceptable: that is a genuine source outage.
    - **Seed/organic overlap decision:** when a seeded item and an organic target item are the same track, cross-source dedup keeps the richer organic copy and `seeded_by` is dropped (`_merge_group` keeps the winner's raw_metadata; `seeded_by` is deliberately NOT added to `_MERGE_BACKFILL_KEYS`). The `seeded` tag therefore measures *seed-exclusive* discoveries — the honest counterfactual ("what did seeding surface that static queries didn't").
    - **Pool stickiness decision:** `raw_metadata` round-trips through `PoolRecord`, so a seeded candidate re-injected from the pool in a later week still carries `seeded_by`. Correct — its exposure remains attributable to seeding.
  - `ranker.py` `_score` — after the liked_label block:

```python
    # Zero-weight measurement tag (Slice C): a seeded candidate carries a
    # 'seeded' signal so the existing by-signal lift table measures whether
    # taste-seeded fetching converts better than static queries. Contributes
    # NOTHING to any score and is never tuned.
    seeded_by = c.raw_metadata.get("seeded_by")
    if seeded_by:
        c.signals.append(RecommendationSignal(
            code="seeded",
            explanation=f"Fetched because you liked {seeded_by}.",
        ))
```

  - `report_artifact.py` `_track_payload`: add `"seeded_by": c.raw_metadata.get("seeded_by"),` alongside the other raw_metadata passthroughs.
  - `src/web/schemas.py`: add `seeded_by: str | None = None` to the report track model (`ReportTrack` or equivalent) — pydantic silently strips unknown keys from `GET /api/reports/{id}` responses, so without the field the artifact value never crosses the API.

- [ ] **Step 4: Run tests** — full suite → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(soundcloud): taste-seeded track search with zero-weight seeded tag"`

### Task C3: Beatport seeded search — time-boxed investigation

**Files:**
- Modify (if feasible): `src/fetchers/beatport.py`
- Test (if feasible): `tests/test_beatport.py`

Beatport currently only walks genre top-100 charts. This task is explicitly conditional per the spec ("implemented if its API cooperates, otherwise dropped without blocking the slice").

- [ ] **Step 1:** Read `src/fetchers/beatport.py` to establish which API host/auth it uses. Check whether the same authenticated session can hit a search endpoint (e.g. `GET /v4/catalog/search?q=<artist>&type=releases` on api.beatport.com — verify the actual path against the fetcher's existing URL patterns; do NOT invent endpoints).
- [ ] **Step 2 (decision gate):** If a search endpoint works with the existing auth: implement seeded search mirroring the C2 pattern (per-seed query, `seeded_by` tag, per-seed try/except, cap `_MAX_PAGES`-equivalent, tests with the file's existing mock style). If it does not (no endpoint, different auth scope, aggressive rate limits): **skip cleanly** — leave `seed_queries` accepted-and-ignored, and record the finding as a code comment on `fetch`'s signature ("seeded search investigated 2026-08: <reason> — SoundCloud carries seeding").
- [ ] **Step 3: Commit** — either `feat(beatport): taste-seeded release search` or `docs(beatport): record seeded-search infeasibility`.

---

## Slice D — surface it

### Task D1: `GET /api/learning` endpoint

**Files:**
- Modify: `src/web/app.py` (after `/api/config`), `src/web/schemas.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Produces (shape the SPA will generate types from):

```json
{
  "learned": {"label_match": {"multiplier": 1.2, "lift": 1.4, "samples": 23, "updated_at": "...", "gated": false}},
  "tunable_signals": ["bandcamp_discovery", "..."],
  "min_samples": 10,
  "positive_artists": [{"name": "om unit", "strength": 4.0}],
  "positive_labels": [{"label": "astrophonica", "strength": 2.0}],
  "seeded_artist_count": 10,
  "seeded_label_count": 5,
  "feedback_known_count": 7
}
```

- [ ] **Step 1: Write failing test** in `tests/test_web_api.py` (existing authed TestClient fixture): seed `data/` with feedback + history + a `learned_weights.json`; `GET /api/learning` → 200 with the multiplier present, positives sorted by strength descending; unauthenticated → 401/403 (match the file's existing auth assertion). **`gated` must be computed from CURRENT `tune_data` counts, not the stored `samples` snapshot** — stored entries always had `samples ≥ 10` at write time, so a snapshot-based flag would never be true; the desk's "anecdote" branding uses current `non_own`, and the page must agree with it. Test case: learned entry with `samples: 30` but current feedback where that signal's `non_own` is 4 → `gated: true`.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** `schemas.py`: `LearnedSignal` (`multiplier: float, lift: float | None, samples: int, updated_at: str | None, gated: bool`), `PositiveArtist` (`name: str, strength: float`), `PositiveLabel` (`label: str, strength: float`), `LearningResponse` (fields per the JSON above). `app.py`:

```python
    @app.get("/api/learning", response_model=schemas.LearningResponse,
             dependencies=[Depends(require_auth)])
    def learning_view():
        from src.pipeline.feedback import (
            feedback_known_keys, load_feedback, positive_artists, positive_labels,
        )
        from src.pipeline.history import load_history, load_mix_prep_history
        from src.pipeline.learning import MIN_SAMPLES, TUNABLE_SIGNALS, load_learned_weights

        entries = load_feedback(settings.data_dir)
        weekly = load_history(settings.data_dir)
        mix_prep = load_mix_prep_history(settings.data_dir)
        learned = load_learned_weights(settings.data_dir)
        remix_aware = settings.pipeline_remix_aware_identity
        artists = positive_artists(entries)
        labels = positive_labels(entries, weekly, mix_prep)
        # Gate state from CURRENT counts — the same non_own number the desk's
        # "anecdote" branding uses — never from the stored samples snapshot.
        from src.pipeline.feedback import tune_data
        signal_slots = tune_data(weekly, mix_prep, entries)["dimensions"]["signal"]
        return {
            "learned": {
                code: {
                    **entry,
                    "gated": signal_slots.get(code, {}).get("non_own", 0) < MIN_SAMPLES,
                }
                for code, entry in learned.items()
            },
            "tunable_signals": sorted(TUNABLE_SIGNALS),
            "min_samples": MIN_SAMPLES,
            "positive_artists": [
                {"name": n, "strength": s}
                for n, s in sorted(artists.items(), key=lambda kv: -kv[1])
            ],
            "positive_labels": [
                {"label": l, "strength": s}
                for l, s in sorted(labels.items(), key=lambda kv: -kv[1])
            ],
            "seeded_artist_count": settings.pipeline_seeded_artist_count,
            "seeded_label_count": settings.pipeline_seeded_label_count,
            "feedback_known_count": len(feedback_known_keys(entries, remix_aware)),
        }
```

- [ ] **Step 4: Run tests** — full suite → PASS. Also `./venv/bin/python -m pytest tests/ -q` one final time; engine slice complete.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): GET /api/learning exposes learned state and positive affinities"`

### Task D2: Web — regenerate types + client wrapper

**Repo:** tunefinder-web (branch off `develop`, e.g. `feat/insights-engine-changes`)

**Files:**
- Modify: `src/api/types.gen.ts` (generated — never hand-edit), `src/api/client.ts`
- Test: `src/api/client.test.ts` if wrapper tests exist there (follow file conventions)

- [ ] **Step 1:** Start the engine locally from the TuneFinder repo (with Slice D merged into the working branch): `TUNEFINDER_WEB_INSECURE=1 ./venv/bin/python -m tunefinder serve` — then in tunefinder-web: `npm run generate-types`. Verify `LearningResponse` appears in `types.gen.ts`.
- [ ] **Step 2:** Add wrapper in `client.ts` following the exact pattern of `getProfile` (line ~118). The file's private helper is `request<T>(conn, path)` (client.ts:59-75), NOT `apiGet` — mirror `getProfile` verbatim:

```typescript
export function getLearning(conn: Connection): Promise<LearningResponse> {
  return request<LearningResponse>(conn, "/api/learning");
}
```

(re-export the `LearningResponse` type alongside the existing type re-exports.)
- [ ] **Step 3:** `npx vitest run` + `npm run lint` + `npm run build` → all green.
- [ ] **Step 4: Commit** — `feat(api): learning endpoint types + client wrapper`.

### Task D3: Web — "WHAT THE ENGINE CHANGED" insights section + copy updates

**Files:**
- Create: `src/pressing/insights/engineChanges.ts` + `engineChanges.test.ts`, `src/pressing/insights/EngineChanges.tsx`
- Modify: `src/pressing/insights/PressingInsightsPage.tsx` (remove the `"NO AUTO-TUNING"` ticker fact ~line 64; add the section + `getLearning` fetch), `src/pressing/insights/TheDesk.tsx` (~line 61 "measurement instrument" prose → the desk now shows what the engine *did*), `src/pressing/primitives.tsx` (signal tones for `liked_artist`, `liked_label`, `seeded` in the stamp tone map ~lines 34-53)
- Test: colocated vitest

- [ ] **Step 1: Write failing tests** for `engineChanges.ts` — a pure function `buildEngineChanges(learning: LearningResponse)` returning render rows: active multipliers sorted by |multiplier − 1| descending with `{code, multiplier, lift, samples, gated}`; gated signals separated (never shown as "applied"); positive artist/label lists; `feedback_known_count`. Cases: empty learning response → empty state flag; gated-only → no applied rows; copy never shows a multiplier for a gated signal (spec: page must not brand a moving multiplier "anecdote" elsewhere — use `gated` from the API, threshold text from `min_samples`).
- [ ] **Step 2:** Implement `engineChanges.ts` (pure), then `EngineChanges.tsx` following the visual language of the existing runout section (`InsightsRunout.tsx`) — dark-first, `night-*` tokens, violet accent, ledger-row layout. Wire the fetch into `PressingInsightsPage.tsx` alongside the existing four fetches (independent fetch, tolerate failure like the others — the page renders without the section if the endpoint 404s against an older backend).
- [ ] **Step 3:** Remove the `"NO AUTO-TUNING"` ticker fact; replace `TheDesk.tsx` prose with copy acknowledging auto-tuning (e.g. "the desk shows the trims the engine has learned — bounded, explainable, resettable"). Add stamp tones for the three new codes.
- [ ] **Step 4:** `npx vitest run`, `npm run lint`, `npm run build` → green. Verify at 390px and desktop, dark (app is dark-only).
- [ ] **Step 5: Commit** — `feat(insights): WHAT THE ENGINE CHANGED section from /api/learning`.

### Task D4: Web — visual regression + final verification

- [ ] **Step 1:** `npm run visual` before finalising — expect diffs only on insights screens (+ any screen showing new signal stamps). Investigate anything else.
- [ ] **Step 2:** `npm run visual:update`, review the PNG diffs in the commit — the diff is the point.
- [ ] **Step 3:** Full check: `npx vitest run && npm run lint && npm run build` → green.
- [ ] **Step 4: Commit** — `test(visual): re-baseline insights for engine-changes section`.

---

## PRs

1. **TuneFinder** `feat/feedback-loop` → `develop`: slices A, B, C (+ D1 endpoint). PR body: spec link, slice map, rollback table.
2. **tunefinder-web** `feat/insights-engine-changes` → `develop`: slice D2–D4. Note deploy order: SPA first, backend second (per CLAUDE.md runbook).

## Self-Review checklist (run after writing, fixed inline)

- Spec coverage: A (A1–A4), B (B1–B3), C (C1–C3), D (D1–D4), dry-run/replay handling (B3 + replay untouched — it builds weights from settings only, documented limitation), explain parity (B3), gate state in API (D1), copy rules (D3). Covered.
- No placeholders; every code step has real code.
- Type consistency: `positive_artists` returns normalised-name keys — consumed as such in ranker (`normalise_artist(part)` lookup) and seeds; `positive_labels` lowercased keys — consumed via `c.label.lower().strip()`. `tune_data` slot fields (`non_own`, `positive`) match feedback.py:420.
