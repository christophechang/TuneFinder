# TuneFinder feedback loop — design

**Date:** 2026-08-12
**Status:** approved (brainstorm with operator)
**Scope:** TuneFinder engine + tunefinder-web. The .NET catalog API is unchanged.

## Problem

The weekly run captures five feedback outcomes (`bought`, `liked`, `skip`, `own`, `heard`)
into `data/feedback.json`, but scoring reads only `skip` (artist penalty via
`skipped_artists()`, `src/pipeline/feedback.py`). Consequences:

- Positive marks (`liked`, `bought`) never improve future ranking.
- `own`/`bought` tracks are not merged into the known-track exclusion set, so an
  owned track can resurface (the catalog API is the only source of
  `known_tracks.json`).
- Scoring weights are static; the insights page measures per-signal lift but the
  engine never acts on it ("NO AUTO-TUNING" is currently a stated principle).
- The taste profile only re-ranks fetched items; it never influences what is
  fetched (source queries are static genre/tag targets in `settings.yaml`).

Goal: close the loop so the system learns from marks over time and produces
better matches, while every learned effect stays deterministic, bounded,
explainable, and independently rollbackable.

## Design principles

1. **Learned state lives in its own files, never in `settings.yaml`.** Deleting
   the learned file reverts the engine to baseline. Config stays human-owned.
2. **Deterministic learning.** No LLM, no opaque optimisation. Every adjustment
   is a bounded arithmetic step from measured lift, logged and explainable.
3. **Symmetry.** Positive marks build boosts the same way `skip` builds a
   penalty. `bought` outweighs `liked`. `heard` stays neutral — it feeds the
   exposure denominator that keeps lift honest.
4. **Auto-apply.** Learning happens hands-off each weekly run (operator
   decision). Rollback = delete `data/learned_weights.json`. No approval gate,
   no kill-switch flag (YAGNI — file deletion is the kill switch).

## Slice A — positive signals + identity fix (engine)

New derivations in `src/pipeline/feedback.py`, joined to
`RecommendationRecord` (which carries `label`, `genre_tags`, `signal_codes`)
and using the same `normalise_artist`/alias machinery as dedup:

- `positive_artists(feedback, history) -> {artist: strength}` — from latest
  marks per `(history, key)`; `bought` counts 2, `liked` counts 1, capped
  per artist.
- `positive_labels(feedback, history) -> {label: strength}` — same join via
  the record's `label`.
- `feedback_known_keys(feedback) -> set[str]` — dedup keys of tracks whose
  latest mark is `own` or `bought`.

Ranker (`src/pipeline/ranker.py`):

- New signal `liked_artist`: `w_liked_artist × strength`, capped like
  `known_artist`.
- New signal `liked_label`: flat boost per positive label.
- Both emit `RecommendationSignal` explanations → report `signals` + explain
  trace.

Known-track merge: the `filter_known` exclusion set becomes
`known_tracks.json ∪ feedback_known_keys` at run time. No file format changes.

## Slice B — auto-tuning (engine)

New module `src/pipeline/learning.py`:

- Per signal code, compute lift exactly as the insights page does:
  `positive_rate(marks carrying signal) / baseline positive_rate`, with `own`
  excluded from rates, over latest marks joined to history `signal_codes`.
- **Gate:** a signal needs ≥ 10 rated marks (non-`own`) carrying it; below
  that its multiplier is untouched.
- **Update rule per weekly run:**
  `multiplier = clamp(old × (1 + 0.1 × (lift − 1)), 0.25, 4.0)`
  — moves 10% toward measured lift per run; slow, bounded, cannot blow up.
- **State:** `data/learned_weights.json` —
  `{signal_code: {multiplier, lift, samples, updated_at}}`. Deleting the file
  is a full reset to baseline `ScoringWeights`.
- Ranker applies `effective_weight = ScoringWeights.w × multiplier`. Explain
  trace and the Discord run summary report every adjustment, e.g.
  `label_match ×1.12 (lift 1.4, n=23)`.

## Slice C — feedback-seeded fetching (engine)

- Top-K positive artists (K = 10) and top-5 positive labels become dynamic
  fetch queries on sources that support artist/label search (Beatport
  confirmed; other fetchers per capability, verified during implementation —
  unsupported sources skip gracefully).
- Seeded items are tagged (`raw_metadata.seeded_by`) so insights can measure
  whether seeded candidates convert better than static-genre ones.
- Existing dedup / history / known-track filters apply unchanged. Seeded fetch
  failures degrade like any other source failure (source health machinery).

## Slice D — surface it (engine API + web)

Engine: new `GET /api/learning` (bearer-authed like the rest) returning
learned multipliers, positive artist/label affinities, seeded targets, and the
feedback-known merge count.

Web (`tunefinder-web`):

- `npm run generate-types` against a locally running backend.
- Insights: remove the `"NO AUTO-TUNING"` ticker fact; add a
  **"WHAT THE ENGINE CHANGED"** section — learned trims rendered in the
  existing desk metaphor, seeded targets, latest adjustments.
- Deploy order per runbook: SPA first, backend second.

.NET catalog API: no changes.

## Testing

- TDD on `feedback.py` derivations, `learning.py` (convergence, clamps,
  gates, empty-state), and the new ranker signals — matching the repo's
  existing test style/fixtures.
- Web: vitest for the new insights parsing/section logic; visual baselines
  re-recorded for the insights screen.

## Rollout & rollback

Slices land as separate PRs into `develop`, in order A → B → C → D.

| Slice | Rollback |
| --- | --- |
| A | revert PR (derived at run time, no persistent state) |
| B | delete `data/learned_weights.json` (or revert PR) |
| C | revert PR (seeded queries are derived, not stored config) |
| D | UI-only; revert PR |

## Risks

- **Small-sample noise** → ≥10-mark gates, 10% step, hard clamps.
- **Self-reinforcement** (boosted signal → more recommendations → more marks)
  — inherent to any closed loop; clamps bound it and insights makes drift
  visible.
- **Name normalization mismatches** between feedback keys and catalog —
  mitigated by reusing `normalise_artist` + `aliases.yaml` everywhere.
