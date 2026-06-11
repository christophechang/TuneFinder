# Phase 1.5 Implementation Spec — Scoring Hygiene

**Date:** 11 June 2026 · **Parent docs:** `docs/improvement-plan.md` §5 (Phase 1.5) and `docs/scoring-review.md` §2 · **Baseline:** v0.7.0 (deterministic reports live) · **Audience:** the implementing Claude Code session.

Five small, data-independent scoring fixes. Each is wrong at any weights (constants scored as signals, popularity paid twice), so none of them require feedback data to justify. **No weight re-tuning happens in this phase** — weights wait for Phase 2 outcome data.

**Resolved decision (Christophe, 11 Jun 2026):** pool injection is exempt from the release-date window in BOTH run modes. The pool-age penalty handles staleness; mix-prep benefits most from older pool gems.

**Out of scope (do not touch):** all scoring weight constants (`_W_*`, existing caps, `_RECURRING_THRESHOLD`) — the new `_GENRE_MATCH_CAP` introduced by Commit 2 is the one deliberate exception; the recency penalty; the Bandcamp bonus; `src/pipeline/reasons.py` and `report.py` (Phase 1 snapshots must pass unchanged); dedup; fetchers; anything under `data/`, `fixtures/`, `.env`.

Work on `develop`. The checkout may be sitting on `main` — switch before Step 0: `git fetch origin && git checkout develop && git pull --ff-only origin develop && git merge --ff-only origin/main`. Merge `origin/main`, not local `main` — local may be stale. If the `--ff-only` merge refuses, the branches have diverged since this spec was written — stop and ask. The merge is currently a fast-forward: the v0.6.6 volumo hotfix (`src/fetchers/volumo.py` + tests) exists only on `main`, so unmerged `develop` gives a stale baseline and a suite that doesn't match production. Execute commits in order; each leaves the suite green.

---

## Step 0 — capture the baseline (not a commit)

Before any change, on clean `develop`, run and save both outputs aside (e.g. `/tmp/baseline-*.txt` — NOT in the repo):

```
./venv/bin/python -m tunefinder run --dry-run
./venv/bin/python -m tunefinder mix-prep house --dry-run
```

The DoD requires a before/after comparison — this phase changes which tracks surface, and the diff is the review artifact.

Expect runtime writes during these dry-runs: `cmd_run` unconditionally writes `data/source_items.json` and an archive snapshot even with `--dry-run`. That's normal — `data/` is gitignored local state. Leave it alone; never commit or clean it.

---

## Commit 1 — `fix(ranker): exclude 'electronic' from genre scoring`

`"electronic"` is in `_BASELINE_GENRES` and fires on nearly every track — the section-assignment code already exempts it from genre caps for exactly that reason (`_UNCAPPED_GENRES`, ~line 244), yet it still scores +0.5 like a real signal. A signal that always fires is a constant.

1. `src/pipeline/ranker.py`: add near `_BASELINE_GENRES` (~line 27):
   ```python
   # Too broad to be evidence of taste fit — nearly every track carries it.
   # Stays in _BASELINE_GENRES (genre-set augmentation + cap exemption); excluded from scoring only.
   _SCORING_EXEMPT_GENRES = {"electronic"}
   ```
2. In `_score`'s genre block (~line 167): `matching = [g for g in c.genre_tags if g in genres_set and g not in _SCORING_EXEMPT_GENRES]`. Leave `_BASELINE_GENRES`, `_build_genre_set`, and `_UNCAPPED_GENRES` untouched.

**Tests** (`tests/test_ranker.py`): candidate tagged only `["electronic"]` → no `genre_match` signal, no genre score; `["house", "electronic"]` → genre contribution exactly 0.5 and the signal explanation lists `house` only.

---

## Commit 2 — `fix(ranker): cap genre_match at 2 tags`

Cross-source dedup unions genre tags, so multi-store tracks accumulate tags AND earn `cross_source` — the same popularity fact paid twice, and the only uncapped signal in the table.

1. `src/pipeline/ranker.py`: add `_GENRE_MATCH_CAP = 2` next to `_W_GENRE` (~line 56); change the score line to `score += _W_GENRE * min(len(matching), _GENRE_MATCH_CAP)`. Leave the explanation's `matching[:3]` display as-is.

**Tests:** 3 matching tags (post-Commit-1 filtering) → genre contribution exactly 1.0, not 1.5; 1 tag → 0.5 unchanged.

---

## Commit 3 — `fix(ranker): fresh_release threshold 30d → 7d`

The weekly corpus is already date-filtered to ≤28 days, so a 30-day freshness threshold makes `fresh_release` fire on every dated candidate — a constant +0.5, discriminating nothing except pool carryovers (which already pay the pool-age penalty: one fact, charged twice).

1. `src/pipeline/ranker.py` ~line 63: `_FRESH_DAYS = 30` → `_FRESH_DAYS = 7`, comment: `# genuinely just-out; inside a 28-day-filtered corpus, 30 was a constant`.
2. No change to `reasons.py` — it never reads `fresh_release`. Its `Fresh {g}, out {d}` template keys off `genre_match` + `days_old` (0–60) and will keep firing on tracks older than 7 days. Expected, not a regression — do not "fix" it.

**Tests:** 10-day-old candidate → no `fresh_release` signal; 5-day-old → signal + 0.5. Update any existing assertion pinned to 30.

---

## Commit 4 — `feat(ranker): per-section score floor`

Sections currently force-fill to their configured counts whenever candidates exist; the worst pick in an 18-track report sets perceived quality. A thin week should ship a thin report.

1. `config/settings.yaml`, `pipeline:` block: add
   ```yaml
   # Minimum score for a track to occupy a report slot — sections may run short.
   # 0 disables the floor (pre-v0.7.1 behaviour).
   section_min_score: 1.0
   ```
2. `src/config.py`: property `pipeline_section_min_score` → `float`, default `0.0` when the key is absent.
3. `src/pipeline/ranker.py`: in BOTH `_assign_sections.pick()` and `_assign_sections_mix_prep.pick()`, first check inside the loop: `if c.score < min_score: continue` (read once from `settings.pipeline_section_min_score` in the enclosing function). Append the floor value to the existing per-run sections log line so under-filled sections are explainable from logs.

**Tests:** candidates scoring below the floor are skipped and the section runs short of `n`; floor `0.0` reproduces current behaviour exactly; applies in mix-prep sections too.

**README:** document the new key in the configuration section.

---

## Commit 5 — `fix(pipeline): exempt mix-prep pool injection from release-date window`

The weekly run injects pool candidates without the window; mix-prep applies it (`tunefinder/__main__.py` ~lines 307–308) — an undocumented inconsistency. Per the resolved decision above, exempt is correct in both.

1. `tunefinder/__main__.py`, `cmd_mix_prep`: delete the `if window_days: _pool = filter_release_date(_pool, window_days)` lines. Keep the genre and genre-exclusion filters on `_pool`. Add comment: `# Pool injection is deliberately exempt from the release-date window (same as the weekly run) — the pool-age penalty handles staleness. See docs/scoring-review.md §2.5.`

**Tests:** none directly (CLI wiring isn't unit-tested); behaviour is reviewed via the DoD dry-run comparison.

---

## Commit 6 — `docs: v0.7.1 changelog and README scoring updates`

1. `CHANGELOG.md`: `## v0.7.1` — the four scoring-hygiene changes + the pool-window alignment, one-line rationale each, pointer to `docs/scoring-review.md`. Patch bump is correct: behaviour tuning, no interface change. (Version lives only in CHANGELOG — verified in Phase 1.)
2. `README.md`: "Scoring signals" section — genre match capped at 2 tags and `electronic` excluded, freshness now ≤7 days, new `section_min_score` key, mix-prep pool note.

---

## Definition of done (run all, paste outputs)

```
./venv/bin/pytest tests/ -v
./venv/bin/python -m tunefinder check-config
./venv/bin/python -m tunefinder run --dry-run
./venv/bin/python -m tunefinder mix-prep house --dry-run
```

Suite green. **The Phase 1 snapshot tests in `tests/test_report.py` must pass UNCHANGED — they build sections directly, so no scoring change can legitimately touch them; if one fails, stop and ask.** Paste both dry-run outputs alongside the Step 0 baselines with a short commentary: which tracks moved in/out and which commit explains each difference. Expected shape of the diff: genre-noise-only tracks gone or demoted, possibly under-filled sections (the floor working as designed), and movement among tracks that previously scored on `fresh_release` alone. Report wording is NOT a check here — dry-run output shows reason text, not signal codes, and `Fresh {genre}, out {d}` lines legitimately persist on older tracks (the composer keys off `genre_match` + `days_old`, not `fresh_release`). The "no `fresh_release` signal past 7 days" guarantee is enforced by Commit 3's unit tests, not the diff. Never post live to Discord. Stop there — no deploy; Christophe triggers "deploy release" himself.
