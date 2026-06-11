# Ranker and Prompt Tuning — Design

Date: 2026-05-17
Status: Draft

## Goal

Improve weekly Discord report quality end-to-end without architectural change. Tune the deterministic ranker so picks better reflect catalog taste, and tighten Stage 1 / Stage 2 LLM prompts so reasons and prose read distinct, factual, and unblurby.

Scope: `src/pipeline/ranker.py`, `src/pipeline/report.py`, `src/pipeline/history.py`, `src/pipeline/pool.py`, `src/llm.py`, `src/models.py`, `tunefinder/__main__.py`, `config/settings.yaml`, plus a new `tests/` tree and `requirements-dev.txt`. One new dev-time dependency: `pytest` (test framework — not loaded at runtime). No new runtime dependencies. No new LLM calls. No config-key additions (only one existing value bumped — Stage 2 temperature).

## Non-goals

- No LLM curator/selection stage (rejected during brainstorming — option 3).
- No new ranker signals beyond those listed below.
- No fetcher changes.
- No Discord output format changes.

## Current state (summary)

- `src/pipeline/ranker.py` is deterministic: weighted signals (known artist, label, cross-source, genre, freshness, chart, Bandcamp) → score → section split (top_picks / label_watch / artist_watch / wildcards) with per-artist, per-release, per-genre caps.
- `src/pipeline/report.py` runs two LLM stages:
  - Stage 1: per-track reason enrichment (one JSON batch call), label synopsis enrichment (cached).
  - Stage 2: full Discord report formatting (one call).
- Fallbacks: signal-derived reasons if Stage 1 fails; `_fallback_report` if Stage 2 fails.

## Design

### 1. Ranker tuning (`src/pipeline/ranker.py`)

#### 1a. Catalog-augmented genre set

Replace module-level constant `_OUR_GENRES` with a runtime-built set:

- Baseline: keep the curated 8-genre set as the minimum floor.
- Augment: union in any genre that appears in `profile.genres_seen` across ≥3 distinct ArtistProfile entries.
- Threshold constant: `_GENRE_AUGMENT_MIN_ARTISTS = 3`.

The augmented set is built inside `rank_candidates` / `rank_candidates_mix_prep` from the profiles dict and threaded through to `_score`.

Usage by entry point:
- `rank_candidates` (weekly): augmented set drives BOTH the genre-match scoring signal and the per-section genre cap in `_assign_sections`.
- `rank_candidates_mix_prep`: augmented set drives ONLY the genre-match scoring signal. Mix-prep has no per-genre cap by design (the genre is already filtered upstream) — `_assign_sections_mix_prep` is unchanged.

#### 1b. Scaled label signal

Replace flat `_W_LABEL_MATCH = 2.5` with a scaled value computed when building `relevant_labels`:

- Track per-label count of distinct known artists with releases in the candidate set: `label_known_artist_counts: dict[str, int]`.
- Score formula for `label_match`: `1.5 + 0.5 * min(label_known_artist_counts[label], 3)`.
- Range: 1.5 (one known artist on the label) → 3.0 (three or more).
- Replace `_W_LABEL_MATCH` constant with `_W_LABEL_BASE = 1.5` and `_W_LABEL_PER_ARTIST = 0.5`, `_LABEL_ARTIST_CAP = 3`.

#### 1c. Scaled cross-source signal

Replace flat `_W_CROSS_SOURCE = 1.0` with `0.5 * min(len(seen_on), 4)`.

- 1 source: 0 (signal still gated by `len(seen_on) >= 2`).
- 2 sources: 1.0 (matches current behavior).
- 3 sources: 1.5.
- 4+ sources: 2.0.

Replace `_W_CROSS_SOURCE` constant with `_W_CROSS_SOURCE_PER = 0.5`, `_CROSS_SOURCE_CAP = 4`.

#### 1d. Artist-recency penalty

New helper in `src/pipeline/history.py`:

```python
def recent_recommended_artists(data_dir: str, weeks: int = 4) -> set[str]:
    """Return normalised artist strings recommended within the last `weeks` weeks
    across BOTH weekly history (recommendation_history.json) and mix-prep
    history (mix_prep_history.json). The two histories live in separate files
    but both represent tracks the DJ already saw — both should suppress repeats.
    """
```

Implementation: load both `load_history` and `load_mix_prep_history`, filter by `recommended_at` within window, split artists via `_split_artists`, normalise via `src.pipeline.dedup.normalise_artist`. A collab record contributes each member.

In `_score`, after the artist-cap math, if any matched profile name (after `normalise_artist`) is in the recent set, subtract `_W_RECENCY_PENALTY = 0.75` from the score and append a `RecommendationSignal(code="recent_recommendation", explanation=...)` so the signal trail explains the down-weight. Penalty applies once per track, not per matched artist.

#### 1e. Pool age penalty (revised from "pool decay")

> Revised after code review: original "decay on load_pool" was a no-op because `pool_to_candidates` discards `last_score` and every pool candidate is rescored from scratch each run. Decay must influence scoring, not loaded score.

Approach: apply a small age-based penalty during scoring for any candidate that came from the persistent pool, based on its `added_at`.

- Thread `added_at` (or weeks-since-added) onto the Candidate during `pool_to_candidates`. Add an optional field to `Candidate`: `pool_added_at: Optional[str] = None`. Fresh-fetched candidates leave it `None`.
- In `_score`, if `pool_added_at` is set, compute `weeks_old = max(0, floor((now - added_at).days / 7))` and subtract `_W_POOL_AGE_PER_WEEK * weeks_old` from the score. Cap penalty at `_POOL_AGE_PENALTY_MAX = 1.5`. The `max(0, …)` clamp prevents a future-dated `added_at` (clock skew, manual data edit) from producing a negative penalty that would inadvertently boost the score.
- Constants: `_W_POOL_AGE_PER_WEEK = 0.25`, `_POOL_AGE_PENALTY_MAX = 1.5`. After 6 weeks the penalty hits the cap.
- Parse `added_at` defensively with `datetime.fromisoformat`; on parse failure skip the penalty.

This lets old pool entries lose ground to fresh material without erasing genuinely strong candidates outright. POOL_CAP trim continues to use the (now-penalised) current score in `__main__.py`.

### 2. Stage 1 reason prompt (`src/pipeline/report.py`)

#### 2a. Richer per-track payload

Replace the current `_enrich_reasons` payload with a fact-only structure (no pre-formatted explanation strings — those bias the LLM toward paraphrasing):

```python
{
  "artist": c.artist,
  "title": c.title,
  "label": c.label or "",
  "source": c.source,
  "genre_tags": c.genre_tags[:5],
  "release_date": c.release_date or "",
  "chart_position": c.raw_metadata.get("chart_position"),
  "cross_source_count": len(c.raw_metadata.get("seen_on_sources", [c.source])),
  "signal_codes": [s.code for s in c.signals],
  "known_artist_play_count": <best_play_count or null>,
  "prior_titles_sample": <up to 3 titles from the matched ArtistProfile, or []>,
}
```

`known_artist_play_count` and `prior_titles_sample` are populated only when `signal_codes` includes `known_artist`. Computed by passing `profiles` (or a derived map) into `_enrich_reasons`. This adds one parameter to `_enrich_reasons` and is threaded from `generate_report` / `generate_mix_prep_report`.

#### 2b. Tighter system prompt

Replace the Stage 1 system prompt with:

> You write concise music discovery reasons for a DJ. [_DJ_CONTEXT]
> For each track, write one sentence (max 15 words) explaining why it fits this DJ's taste.
> Anchor on one concrete fact from the payload: a prior track by the artist, a genre tag, the chart position, or the cross-source count. Use only facts present in the payload.
> Avoid marketing words: sonic, undeniable, journey, vibes, must-hear, perfect for, your next favorite.
> Return a valid JSON array only, no preamble.

Note: "label scene context" was removed as an anchor option after code review. Label synopses are not available at the time `_enrich_reasons` runs (synopsis enrichment happens after, and only for `label_watch` entries). Including the anchor would invite hallucinated label facts. If we want label-aware reasons in a future pass, that's a separate change (e.g. pre-warm the synopsis cache before reason enrichment, or pass cached-only synopses into the payload).

#### 2c. Two-shot anchor

Append two input→output examples to the user prompt (above the actual payload). Examples cover:

- `known_artist` + `chart_position`.
- `label_match` + `genre_match`.

Example reasons should be 10–14 words, name a concrete fact, and avoid the banned words.

#### 2d. Temperature 0.3 (reason enrichment only)

Decision (after code review): per-call override. Reason enrichment benefits from a small temperature bump for varied phrasing. Label synopses are factual and should stay at the conservative default — bumping the global Stage 1 temperature would weaken synopsis grounding.

Implementation: extend `call_stage1` to accept an optional `temperature: float | None = None` override. When passed, it wins over the settings value. `_enrich_reasons` passes `0.3`; `_enrich_label_synopses` passes nothing (uses default). No `settings.yaml` change for Stage 1.

### 3. Stage 2 report prompt (`src/pipeline/report.py`)

#### 3a. Inject this-week stats

In `generate_report`, build a stats summary before the Stage 2 call:

```
This week:
- {n} tracks across {m} labels
- {k} known artists in the set
- Top genres in report: {top1}, {top2}, {top3}
```

Append to the user prompt above the section text. `generate_mix_prep_report` injects only the totals and top genres line (the genre is already known by design).

The LLM may reference these or ignore them — no instruction forces use.

#### 3b. Voice anti-patterns

Append to the Stage 2 system prompt (both `generate_report` and `generate_mix_prep_report`):

> Avoid marketing words: sonic, undeniable, journey, vibes, must-hear, perfect for, your next favorite. No filler intro before sections. No closing summary.

Keep all current format rules verbatim (URL angle brackets, section emojis, label sub-headers, etc.).

#### 3c. Temperature 0.3

`config/settings.yaml`: `llm.stage2.temperature: 0.3` (was 0.2).

### 4. Testing and guardrails

- Validate with `./venv/bin/python -m tunefinder check-config`.
- Use `--dry-run` for pipeline runs; never post live to Discord during development.
- Existing fallbacks remain functional:
  - Stage 1 failure → signal-derived reasons (`c.primary_reason`).
  - Stage 2 failure → `_fallback_report`.
  - Empty signals → "Interesting new release."
- No new dependencies. No new LLM calls.
- One narrow config change: Stage 2 temperature in `settings.yaml` (3c). Stage 1 config is unchanged — reason enrichment uses a call-site temperature override (2d).

## File-level change list

- `src/pipeline/ranker.py` — augmented genre set, scaled label/cross-source signals, recency penalty, pool age penalty, weight constants renamed. New params: `recent_artists: set[str]` threaded into `_score`. Builders accept `data_dir` (via settings) to load recent set.
- `src/pipeline/history.py` — new `recent_recommended_artists` helper reading both weekly and mix-prep history.
- `src/pipeline/pool.py` — populate `Candidate.pool_added_at` in `pool_to_candidates`. No decay-on-load.
- `src/pipeline/report.py` — `_enrich_reasons` payload + prompt rewrite, two-shot anchors, Stage 2 stats injection + voice anti-patterns. `_enrich_reasons` gains a `profiles` parameter. `generate_report` and `generate_mix_prep_report` gain a `profiles` parameter forwarded to `_enrich_reasons`.
- `src/llm.py` — `call_stage1` gains optional `temperature: float | None = None` override.
- `src/models.py` — `Candidate` gains `pool_added_at: Optional[str] = None`.
- `tunefinder/__main__.py` — pass `profiles` to `generate_report` (line ~198) and `generate_mix_prep_report` (line ~366).
- `config/settings.yaml` — Stage 2 temperature 0.2→0.3 only. Stage 1 settings unchanged (overridden per call).

## Risks

- Catalog-augmented genres could include noisy genres if catalog has tag pollution. Mitigation: ≥3-artist threshold.
- Recency penalty interacts with the existing track-level history filter. The track-level filter still applies first (hard); the penalty only affects scoring within remaining candidates.
- Temperature 0.3 on Stage 1 reason calls risks slightly more JSON parse failures. Existing `_clean_llm_json` plus signal fallback covers this. Label synopsis calls stay at the conservative default.
- Pool age penalty hits 1.5 after 6 weeks, which is enough to drop a borderline pool entry but won't suppress a genuinely strong recurring signal (known artist with high play_count still scores well above the cap).
- Threading `profiles` into `generate_report` is a signature change. Existing fallbacks (`_fallback_report`, `_fallback_mix_prep_report`) don't take `profiles`, so they remain unaffected.

## Out of scope (deferred)

- LLM curator/re-ranking stage.
- Re-deriving baseline genres entirely from catalog (currently augment only).
- Label scene-recency scoring (e.g., labels with fresh-release momentum boosted further).
- Per-source weight calibration.
