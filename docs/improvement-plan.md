# TuneFinder — Personal Improvement Plan

**Date:** 11 June 2026
**Decision context:** SaaS direction dropped (see `discovery-report.md`). TuneFinder stays a single-user Python tool, optimised for Christophe. This plan consolidates every improvement suggestion raised during discovery, specifies the two agreed changes in detail (deterministic reports, genre-mapping cleanup), and lists all verified code/config drift.

**Standing corrections:** Volumo audio previews **do work** (user-tested 11 Jun 2026) — the spike doc and README claim otherwise and need erratum/update. Volumo supplies ~72% of the weekly corpus (4,256 of 5,893 items, W23), which matters for several items below.

---

## 1. Goals and non-goals

**Goals:** maximum personal value per hour of maintenance; deterministic, testable output; fewer external dependencies to babysit; evidence-based tuning instead of vibes.

**Non-goals:** no .NET rewrite (no longer justified), no multi-user anything, no new recurring costs, no architectural restructuring for its own sake.

---

## 2. Phase 1a — Deterministic report generation (replaces LLM stages)

**Rationale (trade-offs accepted):** Stage 2 already behaves as a template engine under strict prompt constraints, with `_fallback_report` as its deterministic twin and `_sanitize_report` cleaning up its mistakes. Stage 1 reasons are templated in practice ("X's sixth prior track…") and the label synopses are the one documented hallucination source (cache wiped in v0.6.0). Cost saving is real but small (cascade is mostly free-tier); the actual wins are: no JSON parse failures, no cascade exhaustion or 120s timeouts, snapshot-testable output, zero hallucination risk, four fewer API keys. Accepted loss: prose variety for an audience of one; re-addable later as an optional garnish layer.

### Design

**Renderer** — promote the existing fallback renderers to the only renderers (new `src/pipeline/render.py`, or slimmed `report.py`). Keep verbatim: the two-line track format (`**Artist — Title** [Label] [Source] → [Listen](<url>)` + `> reason`), label-grouped Label Watch, emoji section headers, mix-prep header, `_build_footer` funnel stats + fetcher health, and `_sanitize_report` (still useful for link hygiene). Add: continuous track numbering across the whole report — one counter, never resets (enables the Phase 2 `mark` command; the spec is authoritative here).

**Reason composer** — new `src/pipeline/reasons.py`, pure function of (Candidate, profiles, label_artist_counts):

- Primary-signal precedence: `known_artist` > `label_match` > `chart_position` > `cross_source` > `bandcamp_discovery` > `genre_match` > `fresh_release`.
- One sentence, ≤ ~15 words: primary fact plus at most one secondary fact. Every token comes from data — nothing invented.
- 2–3 phrasing variants per signal combination, selected by stable hash of the track key (variety without randomness; reproducible for tests).
- Available facts: play_count, up to 2 prior titles from the profile, label + count of known artists on it, chart position/source, cross-source count + names, genre tags, days since release.
- Examples of target output:
  - "You've played Sully in 4 mixes (Swandive, Glasshouse) — now #3 on Beatport breaks."
  - "Ilian Tape — 3 artists you play release here. Tagged electronica/breaks."
  - "Picked up by 3 stores this week (Beatport, Volumo, Bandcamp)."
  - "Fresh UKG on Time Is Now, out 5 days ago."

**Label line** — replace the LLM synopsis with catalogue-derived fact: "*{n} of your artists release here: {names}*". The ranker already computes per-label known-artist sets; thread the names through instead of discarding them. (Optional later: factual enrichment from Discogs/MusicBrainz free APIs — never LLM recall.)

**Removals:** `src/llm.py`; `src/pipeline/label_cache.py` usage (leave `data/label_profiles.json` on disk, untouched); `llm:` block in `settings.yaml`; Mistral/OpenRouter/Groq/Gemini env keys from required-config validation; the cascade display in `check-config` (replace with "report mode: deterministic"); `tests/test_llm.py`.

**Note for implementation:** this intentionally supersedes the `CLAUDE.md` guidance to "keep LLM provider logic in `src/llm.py`" and "preserve LLM fallback behavior" — removal of the LLM layer is the point. Update `CLAUDE.md` in the same pass.

**Tests:** snapshot tests for weekly and mix-prep renderers (fixed candidate fixtures → exact expected Discord text); matrix tests covering every reason-composer branch and variant; existing `test_report.py` rewritten.

---

## 3. Phase 1b — Genre mapping cleanup

**Problem:** loose-fit mappings inject wrong-scene tracks. Worst offender confirmed by user: Volumo "Bass House / Future House" (id 2) mapped to `uk-bass` — bass house is not UK bass. With Volumo at 72% of corpus, its loose fits dominate the noise budget. v0.6.6's compilation-genre fix was a symptom of the same class of problem.

**Changes to `config/settings.yaml` (volumo block):**

- Remove `uk-bass ← id 2` (Bass House / Future House). Volumo then honestly has no uk-bass coverage; Mixupload's `/genres/UKBass` and Bandcamp's `uk-bass` tag remain the real sources.
- Remove `hip-hop ← id 29` (Soul / R&B / Hip-Hop) — loose fit, overlaps id 17's territory.
- Remove `funk-soul-jazz ← id 17` (Nu-Disco / Soul / Funk) — loose fit; nu-disco leans house, soul overlaps id 29.
- Keep `downtempo ← id 18` (Organic House / Downtempo) — adjacent but acceptable; monitor.

**README:** update the genre-coverage table to match (Volumo column: uk-bass/funk-soul-jazz/hip-hop → "—"); remove the loose-fit footnote entries that no longer apply.

**Mechanism for the future (specify, don't build yet):** if coverage volume is ever missed, reintroduce loose fits with an explicit `fit: loose` flag per mapping, where loose-fit tags qualify a track for candidate inclusion but are excluded from `genre_match` scoring and from mix-prep `filter_genre`. Until then, deletion is simpler and honest.

**Residual decision for Christophe:** Beatport `rb` chart → `funk-soul-jazz` is also a stretch (R&B chart ≠ funk/soul/jazz) but is the only Beatport feed for that tag. Keep or drop — owner's call.

**Stale-data note:** pool entries (`candidate_pool.json`) tagged via removed mappings will linger up to the age-out window. Acceptable to let them decay naturally (pool-age penalty + 500 cap), or run a one-off purge of pool records whose `genre_tags` only matched via removed mappings. Recommend: natural decay, no data surgery.

---

## 4. Phase 1c — Drift and dead code (all verified, 11 Jun 2026)

1. **`pipeline.max_candidates`** (`settings.yaml`) + `Settings.pipeline_max_candidates` (`src/config.py:117`) — loaded, never consumed anywhere. Remove both.
2. **`LabelRelevance`** dataclass (`src/models.py:58`) — zero usages. Remove.
3. **`Candidate.is_known` / `Candidate.is_previously_recommended`** (`src/models.py:102–103`) — never set, never read (filters remove candidates instead of flagging). Remove.
4. **`ArtistProfile.associated_labels`** — never populated anywhere; all stored profiles show `[]`. Remove field + persistence round-trip; reintroduce properly with the label-memory feature (Phase 4) if/when built.
5. **`sources.discogs` block** in `settings.yaml` — no `discogs.py` fetcher exists. Remove.
6. **`requirements.txt`: `trafilatura`, `schedule`** — neither imported anywhere. Remove (launchd does the scheduling).
7. **`fetch_all_mixes()`** (`src/fetchers/catalog.py:96`) — zero callers; `Mix.bpm_min/bpm_max/energy/moods` modelled but unused. **Keep** — it is the ingestion point for Phase 3 (BPM/energy-aware mix-prep). Add a comment referencing this plan so it stops looking like drift.
8. **Doc drift — Volumo previews:** README sources table and `docs/spikes/2026-06-04-volumo-source-spike.md` claim no previews; user testing says previews work. Fix README; add a one-line erratum at the top of the spike doc (spikes are point-in-time records — don't rewrite history, annotate it).
9. **Juno** — site permanently dead (June 2026), but fetcher + genre_map + config remain. Recommend deleting fetcher and config block (git history preserves it); at minimum annotate as defunct.
10. **Dirty known-track keys** — e.g. `"15. zero t||sonic bionic"`: tracklist numbering leaked into artist names from the upstream catalog API. Add a guard in profile building (strip `^\d+[.)]\s+` from artist names, log occurrences). Root cause lives in the `api.changsta.com` tracklist extraction — fix there too, separately.

---

## 5. Full suggestion backlog (consolidated, prioritised)

**Phase 1 (agreed scope — the new-thread work):** 1a deterministic reports · 1b genre mapping cleanup · 1c drift removal · two small enablers pulled forward: continuous track numbering in reports (for the future `mark` command) and gzip'd weekly source-snapshot archiving (for future replay/backtesting).

> **Implementation spec:** `docs/phase1-implementation-spec.md` in the repo (11 Jun 2026) — commit-by-commit instructions, reason-template table, interface changes, and test plan. The implementing session executes the spec; this plan is context.

**Phase 1.5 — Scoring hygiene (quick wins from `docs/scoring-review.md` §2; run immediately AFTER Phase 1 as its own pass):**

Deliberately not folded into Phase 1: that phase rewrites reporting, and mixing scoring changes in would make the dry-run review diffs unattributable. Each item below is data-independent (defensible without feedback), config-or-small-function sized, and ships with tests plus a before/after dry-run comparison against the Phase 1 deterministic baseline.

- **Exclude `electronic` from the scoring genre set** (keep it in `_BASELINE_GENRES` for the cap-exemption logic). It fires on nearly everything — a constant, not a signal.
- **Cap `genre_match` at 2 tags.** Cross-source dedup unions tags, so multi-store tracks currently get paid for popularity twice (cross_source already rewards it).
- **Re-threshold `fresh_release` from ≤30d to ≤7d.** Inside a 28-day-filtered corpus the current signal is a constant +0.5; at ≤7d it means "genuinely just out". (Keeps the Phase 1 reason-template rows alive — they key on the days_old fact.)
- **Per-section score floor** — new config `pipeline.section_min_score` (suggest 1.0, conservative): sections stop force-filling with genre-noise-only tracks; a thin week ships a thin report. Cheapest perceived-quality win identified in the review.
- **Decision for Christophe:** pool-injection date window — the weekly run exempts pool candidates from the release-date window, mix-prep applies it. Pick one behaviour (recommend: exempt in both; the pool-age penalty already handles staleness) and document it either way.

Explicitly NOT in this pass: weight re-tuning, recency-penalty resize, Bandcamp prior redesign, short-name match guard, two-axis Wildcards — all wait for Phase 2 feedback data per scoring-review §3–4.

**Phase 2 — Observability & feedback (highest leverage after P1):**

- **Fetcher anomaly alerts.** Bandcamp died silently for ~3 weeks. Persist per-run per-source counts; post to the alert channel when a source errors or drops >50% vs its trailing 4-run average. Cheap insurance against the domain's defining failure mode.
- **Retry/backoff in `common.py`.** Verified: single attempt, no retries. Add bounded retry with jitter for transient HTTP failures (not for 403/Cloudflare — those should alert, not hammer).
- **Feedback capture.** `tunefinder mark <track-number|"artist - title"> bought|liked|skip|own` writing `data/feedback.json` (report numbering from P1 makes this ergonomic). Then `tunefinder stats`: hit-rate by signal, source, genre over time. This converts weight tuning from vibes to evidence — the single biggest quality unlock available at n=1. (Discord-reaction harvesting is a later alternative; CLI first, no bot-intent changes.)
- **Implicit feedback via library diff (preferred long-term over manual marking).** Periodically scan the Rekordbox collection (XML export or db) and/or the purchase downloads folder, and diff against recommendation history: a recommended track that later appears in the library is an automatic "bought" signal with zero user effort. Manual `mark` remains for skip/like; the highest-value outcome (purchases) self-records. Also doubles as the first concrete step toward Phase 3's library awareness.
- **`tunefinder explain "<artist> - <title>"`.** Trace any track through the pipeline: fetched from where, deduped into what, dropped by which filter, scored what and why, or why it never surfaced. The deterministic pipeline makes this nearly free to build, and it is the best possible tool for tuning weights and trusting the system.
- **Audition queue output.** Alongside the Discord report, emit a local HTML page with embedded players where stores allow (Bandcamp embeds, Beatport preview widgets, Volumo preview links — previews confirmed working) so a full report can be auditioned in minutes instead of 18 browser tabs. Zero recurring cost; arguably the single biggest workflow upgrade available.
- **Daily `healthcheck` command** (launchd cron): one cheap parse-check per source against a known-good page plus count-drift detection, posting to the alert channel. Subsumes the anomaly-alert item above into a proactive check rather than a post-hoc one.

**Phase 3 — Digging power:**

- **BPM/key in mix-prep.** Volumo (72% of corpus) already supplies BPM + key; Beatport supplies BPM. Add `--bpm 170-180` and key filters/display to mix-prep, wire `fetch_all_mixes` BPM/energy data into profile context. Camelot-compatibility filtering once key coverage is proven.
- **Remix-aware track identity.** Current normaliser strips all version suffixes: owning "Track (Original Mix)" suppresses every future remix of it, and vice versa. Change dedup to keep a version qualifier — merge true duplicates (Original/Extended/Radio), keep named remixes distinct — and rebuild `known_tracks`. Highest-care change in the plan (CLAUDE.md flags this area as regression-prone); ship with thorough tests.
- **Volumo preview exploitation.** Previews confirmed working — no code change needed for links, but stop treating Volumo as metadata-only when weighing sources.

**Phase 4 — Later / optional:**

- **Label memory.** Accumulate artist→label associations observed across runs (finally populating a re-added `associated_labels`), so Label Watch fires even when the known artist isn't releasing that week. Today's label relevance is amnesiac (derived per-run).
- **Alias map.** Small manual `config/aliases.yaml` merged into profile matching (electronic artists are alias-heavy; exact-string matching misses them).
- **Volumo dominance control.** One source is 72% of the corpus; consider per-source contribution caps in section assignment so wildcards aren't structurally Volumo.
- **`tunefinder purge-source <name>`.** The Juno shutdown cleanup was manual JSON surgery across three files; make it a command for next time.
- **Label enrichment via Discogs/MusicBrainz APIs** (factual, free) if label flavour text is missed after synopses go.
- **Optional LLM garnish toggle** (off by default) if deterministic prose ever feels too dry — deliberately last, and only as decoration over deterministic facts.
- **Scene-graph one-hop discovery.** Treat label rosters as a graph: artists who share labels with artists you play earn a small deterministic signal — "label-mate of Calibre on Signature". Explainable, free, and the first scoring expansion beyond exact artist matching. (Pairs with label memory above.)
- **Set-builder mode.** Given 2–3 seed tracks from the collection and a BPM target, sequence mix-prep candidates harmonically (Camelot-adjacent keys, BPM gradient). Depends on Phase 3 BPM/key plumbing; Volumo already supplies key signatures for most of the corpus.
- **Offline replay (`tunefinder replay --week 2026-W20`).** Re-run ranking over an archived weekly snapshot with modified weights to see what would have surfaced — the cheap backtesting harness from the discovery report. Enabled by the Phase 1 archiving commit; build when feedback data exists to compare against.

---

## 6. Validation and rollout rules (all phases)

Work on `develop`. Every change ships with tests. Definition of done per phase: `./venv/bin/pytest tests/ -v` green; `./venv/bin/python -m tunefinder check-config` passes; `run --dry-run` and `mix-prep house --dry-run` produce a sane report (paste output for review); README + CHANGELOG updated in the same pass; no live Discord posts; `.env`, `data/`, `fixtures/` untouched except where this plan explicitly says. Deployment stays manual — Christophe triggers "deploy release" himself.

---

## 7. New-thread kickoff prompt (Claude Code, run from the repo root)

The detailed build instructions live in `docs/phase1-implementation-spec.md` (repo). Paste this to start the implementation session:

```
Read docs/phase1-implementation-spec.md and execute it exactly — commits in the given order, each leaving the test suite green. docs/improvement-plan.md is the parent plan for context; docs/discovery-report.md is background only.

The spec intentionally supersedes CLAUDE.md's LLM-related guidance — removing the LLM layer is the goal, and CLAUDE.md gets updated in Commit 8. Stay strictly within the files the spec names: no opportunistic refactoring, no exploring beyond them, no changes to scoring weights or fetchers other than what the spec lists.

Work on the develop branch, one conventional commit per spec section. If the docs/ files are untracked, commit them first as a docs commit.

Where the spec says "ask Christophe" (the Beatport rb → funk-soul-jazz mapping), or wherever the spec disagrees with the code you actually find, stop and ask before proceeding.

Done means the spec's Definition of Done: ./venv/bin/pytest tests/ -v green, check-config passing with only the Discord env vars required, and you paste the full output of run --dry-run and mix-prep house --dry-run plus both frozen snapshot strings for my review. Never post live to Discord. Don't touch .env, data/, or fixtures/ except where the spec explicitly says. Do not deploy — I'll say "deploy release" myself.
```
