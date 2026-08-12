# TuneFinder feedback loop — design

**Date:** 2026-08-12
**Status:** approved (brainstorm with operator; revised after adversarial review)
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
3. **Symmetry with exclusivity.** Positive marks build boosts the way `skip`
   builds a penalty, and the two are mutually exclusive per artist (see
   Slice A). `bought` outweighs `liked`. `heard` stays neutral and — matching
   the existing lift math in both `tune_data` and the insights page — is
   excluded from lift rates entirely (it is neither positive nor exposure).
4. **Auto-apply.** Learning happens hands-off each weekly run (operator
   decision). Rollback = delete `data/learned_weights.json`. No approval gate,
   no kill-switch flag (YAGNI — file deletion is the kill switch).

## Slice A — positive signals + identity fix (engine)

New derivations in `src/pipeline/feedback.py`, joined to
`RecommendationRecord` (which carries `label`, `genre_tags`, `signal_codes`)
via the existing `(history, key)` join, and using the same
`normalise_artist`/alias machinery as dedup:

- `positive_artists(feedback, history) -> {artist: strength}` — from latest
  marks per `(history, key)`; `bought` counts 2, `liked` counts 1, capped per
  artist. **Exclusivity rule:** an artist with any latest-mark `skip` gets no
  boost (mirror of `skipped_artists()`, which requires zero positives for the
  penalty). One mark can therefore never produce a two-signal swing.
- `positive_labels(feedback, history) -> {label: strength}` — same join via
  the record's `label`, normalised with the ranker's existing
  `label.lower().strip()` convention.
- `feedback_known_keys(feedback) -> set[str]` — exclusion keys of tracks whose
  latest mark is `own` or `bought`. Emits **both** legacy and remix-aware key
  regimes, mirroring `build_known_track_keys` in `profile.py`, so the merge
  behaves correctly whichever `pipeline.remix_aware_identity` is set to.

Ranker (`src/pipeline/ranker.py`):

- New signal `liked_artist`: `w_liked_artist × strength`, capped like
  `known_artist`.
- New signal `liked_label`: flat boost per positive label.
- Both emit `RecommendationSignal` explanations → report `signals` + explain
  trace. (Old SPA bundles render unknown codes as untinted stamps — harmless;
  Slice D adds proper tones.)

**Known-track merge — single point.** The merge of
`known_tracks.json ∪ feedback_known_keys` happens where the known-key set is
produced (`_load_profile_state` in `src/services/runs.py`), so every consumer
— weekly run, mix-prep run, explain, and pool injection — sees the same set.
Pool injection additionally compares raw `Candidate.key`s; it must also check
the normalised `make_dedup_key` form so an owned track sitting in the pool
cannot resurface. No file format changes.

## Slice B — auto-tuning (engine)

New module `src/pipeline/learning.py`:

- Per signal code, compute lift exactly as the insights page does:
  `positive_rate(marks carrying signal) / baseline positive_rate`, with
  `own`/`heard` excluded from rates, over latest marks (both histories —
  ratios are internally consistent because baseline uses the same pool)
  joined to history `signal_codes`.
- **Tunable allowlist.** Only these codes are tuned: `known_artist`,
  `recurring_artist`, `label_match`, `scene_adjacent`, `cross_source`,
  `genre_match`, `fresh_release`, `chart_position`, `bandcamp_discovery`,
  `source_popularity`. Explicitly excluded:
  - penalty codes (`skipped_artist`, `recent_recommendation`, `pool_age`) —
    lift-based tuning is directionally wrong for penalties (a working penalty
    depresses conversion by construction);
  - feedback-derived codes (`liked_artist`, `liked_label`) — measuring them
    with the marks that created them is double-dipping, the strongest
    self-reinforcement channel in the design.
- **Gate:** a signal needs ≥ 10 rated marks (non-neutral) carrying it; below
  that its multiplier is untouched. If overall baseline is 0 (no positives
  anywhere) or lift is undefined, no update occurs.
- **Update rule per weekly run (convergent):**
  `target = clamp(lift, 0.25, 4.0)`;
  `multiplier = old + 0.2 × (target − old)`
  — exponential approach to the measured lift; fixed point is the (clamped)
  lift itself, so multipliers converge instead of ratcheting into the clamps.
  A persistent lift of 1.4 converges to ×1.4, not ×4.0.
- **Application point:** the multiplier scales the signal's **final
  contribution** (after any internal formula or cap, e.g. `known_artist`'s
  `max_artist_score` cap or `label_match`'s base+per-artist sum), not
  individual `ScoringWeights` fields. This keeps composite signals
  (`label_match`, the two `source_popularity` weights) coherent and the
  effect symmetric in both directions. Applied inside the shared `_score`
  path so weekly, mix-prep, and `/explain` all agree.
- **State:** `data/learned_weights.json` —
  `{signal_code: {multiplier, lift, samples, updated_at}}`. Deleting the file
  is a full reset to baseline. **Never written on `--dry-run`.** `replay`
  keeps using baseline weights only (archived-week replays stay reproducible);
  documented as a known limitation.
- Explain trace and the Discord run summary report every adjustment, e.g.
  `label_match ×1.12 (lift 1.4, n=23)`.

## Slice C — feedback-seeded fetching (engine)

Re-scoped after review: **no fetcher currently supports artist/label search**;
Beatport only walks genre charts, and the dispatcher has no per-query seed
concept. So this slice is an interface change plus per-source capability work:

- Extend the fetcher interface with optional seeded queries (top-K positive
  artists, K = 10; top-5 positive labels), threaded through
  `fetch_all_sources`.
- Implement for **SoundCloud first** (its target search already supports
  free-text `q` queries — smallest step).
- **Beatport search is net-new API integration** (search endpoints,
  pagination, rate limits); implemented if its API cooperates, otherwise
  dropped without blocking the slice. Other sources skip gracefully.
- Seeded items are tagged (`raw_metadata.seeded_by`) so insights can measure
  whether seeded candidates convert better than static-genre ones.
- Existing dedup / history / known-track filters apply unchanged. Seeded fetch
  failures degrade like any other source failure (source health machinery).

## Slice D — surface it (engine API + web)

Engine: new `GET /api/learning` (bearer-authed via the existing `require_auth`
dependency) returning learned multipliers **with per-signal gate state**
(`samples`, gated yes/no), positive artist/label affinities, seeded targets,
and the feedback-known merge count.

Web (`tunefinder-web`):

- `npm run generate-types` against a locally running backend.
- Insights: remove the `"NO AUTO-TUNING"` ticker fact **and** the
  "measurement instrument" prose in `TheDesk.tsx`; add a
  **"WHAT THE ENGINE CHANGED"** section — learned trims rendered in the
  existing desk metaphor, seeded targets, latest adjustments. Copy must use
  the per-signal gate state so the page never shows a multiplier moving on a
  signal it elsewhere brands "anecdote, not evidence"
  (`DESK_MIN_RATED_MARKS`).
- Signal tones for `liked_artist` / `liked_label` stamps.
- Deploy order per runbook: SPA first, backend second.

.NET catalog API: no changes.

## Testing

- TDD on `feedback.py` derivations (exclusivity rule, both key regimes),
  `learning.py` (convergence to clamped lift, gates, null-lift no-op,
  allowlist enforcement, dry-run no-write), and the new ranker signals —
  matching the repo's existing test style/fixtures.
- Explain-parity test: a scored candidate and its `/explain` trace agree under
  non-default multipliers.
- Web: vitest for the new insights parsing/section logic; visual baselines
  re-recorded for the insights screen.

## Rollout & rollback

Two PRs into each repo's `develop`: the engine PR carries slices A → B → C as
slice-labeled commit groups (in that order, each group independently
revertable); the web PR carries slice D.

| Slice | Rollback |
| --- | --- |
| A | revert its commits (derived at run time, no persistent state) |
| B | delete `data/learned_weights.json` (or revert its commits) |
| C | revert its commits (seeded queries are derived, not stored config) |
| D | UI-only; revert PR |

## Risks

- **Small-sample noise** → ≥10-mark gates, convergent 20% step, hard clamps.
- **Self-reinforcement** (boosted signal → more recommendations → more marks)
  — the allowlist removes the worst channels (penalties, feedback-derived
  signals); clamps bound the rest and insights makes drift visible.
- **Name normalization mismatches** — reuse `normalise_artist` +
  `aliases.yaml` for artists and the ranker's `lower().strip()` for labels.
- **Mix-prep marks skew weekly tuning** — accepted: lift is a ratio against a
  baseline drawn from the same joined pool, and gating needs the sample depth;
  revisit if mix-prep volume dwarfs weekly.
