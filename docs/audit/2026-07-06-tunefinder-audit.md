# TuneFinder — Discovery Audit

**Date:** 6 July 2026
**Scope:** full repository at v0.9.0 (commit `3ae71f5`, `main` == `develop`), all docs, config, and tests. Focus: how good is TuneFinder at working out what Christophe actually likes and surfacing tracks he'll want — and what limits it.
**Method:** end-to-end code read (every module in `src/`, `tunefinder/`, `config/`, `tests/`), all docs in `docs/`, CHANGELOG, README, AGENTS.md. Test suite verified green (225 passed) in a fresh venv. `data/`, `fixtures/`, and `logs/` are gitignored and not present in this clone — runtime-data claims below are sourced from the repo's own docs (notably `docs/discovery-report.md`, 11 Jun 2026, W23 run data) and flagged as such.

---

## 1. Executive summary

TuneFinder is a mature, disciplined, single-user pipeline: fetch ~5,900 candidate releases/week from four stores, filter against what you own and what it already told you about, score with hand-tuned linear signals against an artist-play-count profile, and post an 18-track explained report to Discord. The engineering hygiene is genuinely good — deterministic output, snapshot tests, explain/replay-friendly state, per-source health alerts, feedback capture (`mark`/`stats`), archived weekly snapshots.

The core finding on discovery quality is that **the system is architected for trust, not discovery — and its taste model is a single column of numbers.** Specifically:

1. **Taste = artist play counts from published mixes, and nothing else.** The profile is `{artist: play_count, genres_seen, track_titles}` derived from public SoundCloud mix tracklists. No label affinity memory, no genre *weighting* (only binary set membership), no BPM/energy/mood usage (the data is fetched-able but the ingestion function has zero callers), no aliases, no recency of taste, no collection/library signal, no negative taste ("never show me X").
2. **The ranking is a familiarity ranking with discovery tiebreakers.** `known_artist` (up to 10.0 + 2.0 recurring) towers over every discovery signal (label 2.0–3.0, cross-source 1.0–2.0, chart ≤1.5, genre ≤1.0). The repo's own scoring review says it plainly: whenever ≥5 known-artist tracks survive, Top Picks is all people you already play, Artist Watch takes the next known artists, and Wildcards fills with overflow. Only Label Watch reliably surfaces unknowns.
3. **The feedback loop is half-built.** `mark`/`stats` capture outcomes and slice them by signal/source/genre — but nothing consumes them. Weights remain folklore; there is no replay harness over the archived snapshots (explicitly planned in `improvement-plan.md` §5 Phase 4, never built); `stats` output is a human-read report, not an input to anything.
4. **The candidate corpus is chart-shaped and one source dominates.** All four active sources are store charts/new-release feeds; chart position is *also* a scoring signal, doubling down on popularity. Volumo supplied ~72% of the W23 corpus (per `improvement-plan.md`). There is no candidate stream that starts from *your* taste (label rosters, artist follow-ups, scene adjacency) rather than from what's charting.
5. **There is no AI anywhere in the pipeline — by deliberate, documented decision** (v0.7.0 removed the two-stage LLM cascade). This is a strength for trust and testability, and a constraint worth respecting: any new intelligence should stay deterministic-first, with LLM/embeddings only as clearly-fenced optional layers.

The chassis (explainable signals, filters, pool, sections, deterministic rendering) is sound and worth building on, not around. The highest-leverage discovery work is: richer taste modelling (label memory, genre affinity weights, aliases), a real exploration channel (two-axis scoring), taste-led candidate sourcing (label/artist watch fetching), and closing the feedback loop (replay + evidence-based weights).

---

## 2. System context — where this sits in OpenClaw

- **Runtime home:** Mac mini at `192.168.1.122`, checkout at `/Users/christophechang/OpenClaw/Automations/TuneFinder`, running `main`. Deploys are manual: "deploy release" = changelog + tag on `develop`, push, then SSH + `git pull` on the mini (per `CLAUDE.md`).
- **Scheduling:** launchd (`com.openclaw.tune-finder.plist`), Sundays 09:00, logs to `logs/launchd.log`. The plist in the repo is a template (`YOUR_ADMIN_USER` placeholder) pointing at a `~/Documents/Development/TuneFinder` path — drift vs. the documented OpenClaw path; presumably the installed copy differs.
- **Upstream dependency:** the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) (`api.changsta.com`) serves the published-mix catalogue (`/api/catalog/mixes`, `/api/catalog/tracks`). This is the *sole* taste input. If it's down on run morning, `fetch_all_tracks` raises on page 1 and the run dies before fetching sources (no graceful degradation for the profile, unlike source fetchers). Note the default base URL is hardcoded in `src/fetchers/catalog.py` (`_DEFAULT_BASE_URL = "https://api.changsta.com"`) with `catalog.user_url` in settings.yaml as an empty-string override — inverted from the "no personal URLs in code" rule.
- **Downstream:** Discord guild, four channels — `#music-research` (weekly report), `#mix-prep`, `#logs`, `#alerts` — via raw bot-token REST calls (`src/output/discord.py`), 2000-char chunking, 429 handling.
- **Local artifacts:** audition HTML pages under `data/reports/` (retained 26), optionally served at `TUNEFINDER_AUDITION_BASE_URL` and linked from the report footer.
- **Branches:** `develop` and `main` both exist on origin and currently point at the same commit.

## 3. Architecture — the pipeline as it runs today

`tunefinder run` (weekly) executes, in order (`tunefinder/__main__.py:cmd_run`):

1. **Profile refresh** — `fetch_all_tracks` pulls the full deduplicated track catalogue from the companion API; `build_artist_profiles` splits collaborator strings and accumulates `play_count` (weighted by `recurrence_count`), `genres_seen`, `track_titles`. Known-track exclusion keys built via `make_dedup_key` (version/feat-stripped). Saved to `data/known_tracks.json` / `data/artist_profiles.json`.
2. **State load** — weekly recommendation history, candidate pool (cap 500).
3. **Source fetch** — `fetch_all_sources` runs enabled fetchers (Beatport, Bandcamp, Volumo, Mixupload; Traxsource/RA/Boomkat/Bleep disabled), each isolated with per-source error capture into a `health` dict. Items saved to `data/source_items.json` and gzip-archived per week (`data/archive/`, 26 retained).
4. **Anomaly detection** — `source_health.detect_anomalies` compares counts to the trailing-4-run mean; errors/zero-counts/drops >50% post to `#alerts`.
5. **Dedup + filters** — `deduplicate_source_items` groups by normalised `artist||title` key, merges richest-metadata winner, unions genre tags, records `seen_on_sources`, backfills embed IDs. Then `filter_known` (owned tracks, incl. nested release tracks), `filter_history` (previously recommended), `filter_release_date` (28-day window; undated items — i.e. some Bandcamp — pass).
6. **Pool injection** — unrecommended past candidates re-enter scoring (exempt from the date window; they carry `pool_added_at` for an age penalty).
7. **Ranking** — `rank_candidates` scores every candidate (see §5), sorts, and assigns sections with diversity caps (per-artist 2, per-release 2, per-genre 3 global, per-section score floor 1.0).
8. **Rendering** — `report.generate_report` builds the Discord text deterministically; `reasons.compose_reason` picks a template row by signal precedence and fills it with catalogue facts; md5-of-key selects phrasing variants. Audition HTML page written on live runs.
9. **Posting + persistence** — report to Discord; recommended tracks appended to history (with track numbers, signal codes, genres, score); pool rebuilt from unselected candidates (top 500 by score, `added_at` preserved); run summary to `#logs`.

`mix-prep <genre>` is the same pipeline narrowed to one genre (fetchers accept `target_genre`), with its own history file, genre + genre-exclusion filters, two sections (Top Picks / Deep Cuts, 20 each), and no genre cap.

Support commands: `check-config`, `save-fixtures`, `build-profile`, `fetch-sources`, `mark <n|"Artist - Title"> <bought|liked|skip|own>`, `stats`, `explain "Artist - Title"` (offline pipeline trace, weekly-only).

**Assessment:** the orchestration lives as two long functions in `__main__.py` (~200 lines each) with the pipeline steps duplicated between `run` and `mix-prep` and re-mirrored a third time inside `explain.py`. It works and is readable, but every pipeline change now has to be made in 2–3 places (explain already has a "mirroring cmd_run" comment). This is the main structural friction for the improvements this audit motivates.

## 4. How it models taste today

The entire taste model is:

| Store | Contents | Used for |
|---|---|---|
| `artist_profiles.json` | per-artist: `play_count` (Σ recurrence over mixes), `genres_seen`, `track_titles` | `known_artist`/`recurring_artist` scoring, label relevance derivation, reason text |
| `known_tracks.json` | normalised keys of every track ever in a published mix | exclusion filter ("don't recommend what I play") |
| genre set (runtime) | 8 baseline genres ∪ any catalogue genre seen across ≥3 artist profiles | binary `genre_match` (+0.5/tag, cap 2), section genre caps |
| `recommendation_history.json` / `mix_prep_history.json` | everything previously recommended | repeat suppression + 4-week artist recency penalty |
| `feedback.json` | manual `mark` outcomes | **displayed by `stats` only — feeds nothing** |

What is *not* modelled, despite data being in reach:

- **Label affinity has no memory.** `_build_relevant_labels` re-derives "labels you care about" each run *from that run's candidate set*: a label counts only if one of your known artists happens to have a release in this week's corpus. Your actual label history (the labels behind your 1,300 known tracks) is never mined; `docs/improvement-plan.md` §5 Phase 4 ("Label memory", "amnesiac") flagged this and it remains unbuilt. A quiet week for Signature = Signature doesn't exist.
- **Genre preference is binary, not weighted.** Playing 400 DnB tracks and 3 downtempo tracks produces identical +0.5 `genre_match` for both. `genres_seen` counts per artist exist; a corpus-level genre distribution is never computed.
- **Mix-level features are fetched-able and ignored.** `fetch_all_mixes()` returns BPM ranges, energy ("peak"/"journey"), moods, per-mix genre and publish dates — zero callers (kept deliberately, per comment, for a BPM/energy-aware mix-prep that was never built). Taste has no tempo, energy, or era dimension.
- **No aliases.** Exact lowercase string match on split artist names. Electronic music is alias-heavy; a Calibre release as "Fracture & Calibre" matches (split works) but any true alias scores zero — and short names risk false positives ("You play X" about the wrong X — the worst trust error; flagged in `scoring-review.md` §2.6, no mitigation exists).
- **No taste recency.** A track mixed in 2019 counts the same as last month. `published_at` exists on mixes; unused.
- **No negative signal.** `skip` marks are recorded and never consulted; there is no "less of this" mechanism of any kind.
- **No collection awareness.** Published mixes under-represent bought-but-not-yet-mixed tracks (the *most recent* taste). The Rekordbox/library-diff idea (improvement-plan §5 Phase 2, "preferred long-term") is unbuilt.
- **Two artist-splitting regexes disagree.** `profile._SPLIT_RE` splits on `,`/`feat`/`&`/`x` (not `/`, not `vs`); `dedup._ARTIST_SEP_RE` splits on `/`/`&`/`x`/`vs` (not `,` — it canonicalises to commas). Mostly harmless, but "A vs B" profiles as one artist while deduping as two.

## 5. Scoring and section assignment (the discovery engine)

Linear additive signals, mutating `Candidate.signals` + `score` (`src/pipeline/ranker.py`):

| Signal | Formula | Range |
|---|---|---|
| `known_artist` | Σ `play_count × 3.0` over matched artists, cap 10.0 | 3.0–10.0 |
| `recurring_artist` | +2.0 if best play_count ≥ 3 | 0/2.0 |
| `recent_recommendation` | −0.75 if matched artist recommended ≤4 weeks | 0/−0.75 |
| `label_match` | 1.5 + 0.5 × min(known artists on label, 3) | 2.0–3.0 |
| `cross_source` | 0.5 × min(sources, 4), needs ≥2 | 1.0–2.0 |
| `genre_match` | 0.5 × min(matching tags, 2); `electronic` excluded | 0.5–1.0 |
| `fresh_release` | +0.5 if ≤7 days old | 0/0.5 |
| `chart_position` | 1.5 × (1 − (pos−1)/100) | ~0–1.5 |
| `bandcamp_discovery` | +1.0 flat for every Bandcamp item | 0/1.0 |
| `pool_age` | −0.25/week, cap −1.5 | 0–−1.5 |

The v0.7.1 hygiene pass (electronic exclusion, genre cap, 7-day freshness, score floor, pool-window consistency) fixed the "scoring constants" class of defect. What remains is *structural*:

- **Familiarity dominance** (§1.2 above). Max discovery-only score ≈ 3.0 (label) + 2.0 (cross-source) + 1.0 (genre) + 1.5 (chart) ≈ 7.5 in theory, ~4–5 typically — vs. 12.0 for any 4-play artist. The −0.75 recency penalty is a 6% haircut on a favourite. The report's four section names promise more differentiation than one score axis can deliver: Wildcards = "highest scoring remainder", i.e. the same familiarity ordering, minus the tracks already taken.
- **Chart position is both a corpus filter and a score bonus** — popularity double-dip. Beatport items *only exist* in the corpus because they charted top-100; they then get up to +1.5 for it. Combined with Volumo's `sort=purchase`, the corpus tilts mainstream within each genre.
- **Bandcamp's +1.0 is a source prior wearing a signal costume** (scoring-review §2.3, still true) and it stacks with Bandcamp's date-window exemption. Note: since the v0.6.3 `discover_web` migration Bandcamp *does* return `release_date`, so both the exemption's rationale and the README's "no dates" claim are partially stale.
- **Mixupload popularity data is captured and unused.** `download_count` / `stream_count` land in `raw_metadata` and are consulted by nothing.
- **BPM/key are captured (Volumo, Beatport, Mixupload), displayed on audition pages, and used by no filter or score.** Mix-prep — the one mode with an explicit DJ intent — cannot say "170–175 BPM" or "Camelot-compatible with 8A".
- **Volumo dominance is unmanaged.** ~72% of corpus (W23); no per-source share caps in section assignment (improvement-plan Phase 4 flagged it). Cross-source and chart signals partially counterweight, but Wildcards especially will be structurally Volumo.
- Section mechanics are otherwise thoughtful: per-artist/release caps reset per section (deliberate), genre cap global, floor 1.0, `used` tracking by `id()`.

## 6. Candidate sourcing — what the corpus can and cannot contain

Active: Beatport genre top-100 charts (`__NEXT_DATA__`), Bandcamp `discover_web` newest-per-tag (20/tag), Volumo REST API (curated, sort=purchase, 28-day lookback, ≤3 pages/tag), Mixupload monthly charts + one genre page. Disabled/dead: Traxsource (Cloudflare), RA (off by default), Boomkat (Cloudflare), Bleep (login). History shows a source dies roughly every quarter; `common.py` retry + health alerts are the (good) mitigation.

Everything is **genre-feed-shaped**: the pipeline asks each store "what's new/charting in genre G" and then tries to recognise taste in the answer. Nothing asks the taste model where to look:

- No label-roster stream ("fetch new releases from the 30 labels my catalogue actually points at" — Bandcamp label pages and Beatport label pages exist and are scrapeable with the same techniques already used).
- No artist-forward stream (known artists' new releases regardless of chart position — a known artist at Beatport #101 is invisible today).
- No scene-adjacency (label-mates of your artists; improvement-plan Phase 4 "scene-graph one-hop" idea).
- Bandcamp fetch is 20 items/tag of "new" — a thin, arbitrary slice of the deepest independent source in the set.
- Genre taxonomy is hand-mapped per source (documented loose-fit problems; `genre_exclusions` patch for contradictory merged tags). This is domain-inherent, but it means corpus quality varies by genre in ways the scoring can't see.

## 7. AI / prompt flow

**There is none — deliberately.** v0.7.0 removed the two-stage LLM cascade (Mistral/Groq/Gemini/OpenRouter reason-enrichment + DeepSeek report-writing) after documented hallucinations (label synopses invented cities/artists — cache wiped in v0.6.0) and JSON-parse/cascade-exhaustion fragility. Reasons are now composed by `src/pipeline/reasons.py`: signal-precedence template table, md5-stable phrasing variants, every token from candidate/profile facts. Snapshot tests freeze exact report output. `check-config` prints "Report generation: deterministic (no LLM)".

Implication for this initiative: "make it smarter" must not mean "put an LLM back in the render path". The guardrails (CLAUDE.md: rendering free of network/LLM deps) are correct. Where model-driven intelligence could legitimately enter later is *upstream and offline*: taste-profile enrichment, alias suggestion, genre mapping maintenance — batch jobs whose outputs are reviewed, versioned data files, never live-run dependencies. Any such item needs a `needs-live-run` flag (home network + API keys).

## 8. Feedback and measurement

- `mark` (CLI + audition-page copy buttons) → append-only `feedback.json`; `own` is tracked separately as an identity-gap signal. `stats` aggregates hit-rate by signal/source/genre/report. Good schema, decent ergonomics.
- **Nothing downstream consumes it.** No weight fitting, no per-signal precision feeding the ranker, no skip-derived suppression, not even a "your label_match positive rate is 80%, genre_match 20%" nudge into the docs.
- **The replay harness — the explicitly-planned backtesting tool (`tunefinder replay --week`) that the archives exist to enable — was never built.** This is the single biggest gap between "captures feedback" and "learns". Archived snapshots + history + feedback are all on disk; the harness is pure offline work.
- Coverage risk: feedback value depends on marking discipline; there is no passive signal (library diff) to backstop it. Unknown how dense `feedback.json` is (file not in repo); the plan should not *hard-depend* on dense feedback existing yet — replay + instrumentation first, weight-fitting when density allows.

## 9. State, tests, and dev infrastructure

- State = flat JSON in `data/` (profiles, known tracks, two histories, pool, feedback, source health) + gzip archives + audition HTML. All load-tolerant of missing files except the catalogue fetch (§2). Histories grow unboundedly (append-only) — fine for years at this rate.
- 225 tests, well-shaped: fixture-driven fetcher parsers (Mixupload HTML fixtures checked in), snapshot tests for report/audition output, pure-function tests for ranker/dedup/reasons/feedback/health. `conftest.py` provides settings/candidate factories. External IO consistently mocked. This is a codebase where scoring changes can be made with confidence, and where "update snapshots deliberately" is the stated (and test-enforced) culture.
- Docs are unusually strong and honest: `discovery-report.md` (SaaS discovery, killed), `scoring-review.md` (the best single critique of the ranker — most of its §2 shipped in v0.7.1; its §3–4 "wait for data" items are exactly what's still open), `improvement-plan.md` (the standing backlog; Phases 1–2b shipped, Phase 3 "digging power" and Phase 4 "later" largely open), plus per-phase implementation specs and spikes.

## 10. Weak points — ranked by impact on discovery quality

1. **One-axis familiarity scoring / no exploration channel.** Sections don't differentiate; Wildcards is a leftovers bin. (Fix shape: two-axis familiarity/discovery scoring — already sketched in scoring-review §4; cheap; no new data needed.)
2. **Amnesiac label affinity.** The strongest available "your scene" signal (labels behind your 1,300 tracks + labels observed across archived runs) is recomputed weekly from a keyhole. Also blocks scene-graph/label-roster ideas.
3. **Feedback loop unclosed; no replay harness.** Every future scoring change is still evaluated by vibes, exactly as the scoring review complained. The archives make this nearly free to fix offline.
4. **Chart-shaped, Volumo-heavy corpus with popularity double-counting.** The tool can only recommend what stores already promote; taste-led sourcing (label rosters, artist follow-ups) doesn't exist; no per-source balance control.
5. **Flat genre model.** No affinity weights, no per-genre corpus normalisation; `genre_match` is nearly uniform within a feed that was already genre-filtered.
6. **Alias blindness + short-name false-positive risk.** Missed releases (invisible) and wrong "You play X" claims (trust-damaging); zero mitigation.
7. **Remix/version identity conflation.** Owning "Track (Original Mix)" suppresses every future remix of that title by that artist (`_VERSION_RE` strip in both dedup and known-keys); conversely distinct works merge. Highest-care area (regression-prone, CLAUDE.md warns), but a real discovery leak for a DJ.
8. **BPM/key/energy unused.** Mix-prep can't filter by tempo/key despite majority coverage; taste model has no tempo/energy dimension despite `fetch_all_mixes` existing.
9. **No taste recency / no negative feedback usage.** The profile is a lifetime average; skips teach nothing.
10. **Structural duplication of the pipeline** (`cmd_run` / `cmd_mix_prep` / `explain`) raises the cost of every change above.
11. **Ops nits:** catalogue-API failure kills the run pre-report (no degraded mode using last-saved profiles); hardcoded default personal URL in `catalog.py`; plist path drift; README drift (Bandcamp "no dates", Mixupload UKBass URL comment, `mix-prep` docs vs `page1` code path).

## 11. What's genuinely good (don't break it)

Deterministic rendering + snapshot tests; explainable `RecommendationSignal` architecture (every point of score has a code and a sentence); the filter stack and dedup key discipline (history keys stored raw *and* normalised); persistent pool with age decay; separate mix-prep history; source-health alerting; archive retention; graceful per-source failure; the docs culture. All new discovery work should keep: deterministic at run time, explainable per-track, config-driven weights, tests-with-change.

---

*Companion document: `2026-07-06-tunefinder-plan.md` (implementation plan derived from this audit).*
