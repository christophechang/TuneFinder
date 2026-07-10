# Changelog

All notable changes to TuneFinder. The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## Unreleased

### Web transformation (issues #14–#16; SPA in [tunefinder-web](https://github.com/christophechang/tunefinder-web))

TuneFinder is now a web application: a FastAPI service in this repo (running on the mini, next to `data/`) plus the tunefinder-web SPA. Architecture and the deliberate divergence from MixLab Anywhere are recorded in `docs/architecture/tunefinder-web.md`; deployment runbook in `docs/ops/web-service.md`.

- **Run services extraction** (#14). `cmd_run`/`cmd_mix_prep` orchestration moved verbatim into `src/services/runs.py` (`run_weekly`/`run_mix_prep` with progress callbacks and a structured `RunOutcome`); the CLI is a thin wrapper — behaviour and snapshots unchanged.
- **Structured report artifact** (#14). Every live run writes `data/reports/report_{id}.json` — sections, deterministic reasons, signals, two-axis scores, BPM/Camelot key, player embed ids, funnel stats. The web app renders reports from it; artifacts are never pruned. Pre-artifact reports render degraded from history records.
- **Write safety** (#14). All JSON stores now write atomically (temp + `os.replace`), and a `data/`-scoped file lock serialises pipeline runs across the web service, launchd, and manual CLI use (contention fails cleanly, exit 1 / HTTP 409).
- **Web API** (#15). `tunefinder serve` (FastAPI/uvicorn, default `127.0.0.1:8420`): reports list/detail with feedback state joined, one-tap feedback marking, feedback stats + structured tune data, explain, profile/pool/source-health/config views, open `/api/health`. Bearer-secret auth (`TUNEFINDER_API_SECRET`, constant-time, fail-closed with an explicit `TUNEFINDER_WEB_INSECURE=1` opt-out), config-driven CORS, optional static SPA mount (`TUNEFINDER_WEB_STATIC_DIR`).
- **On-demand runs** (#16). `POST /api/runs` starts weekly or mix-prep runs (genre/BPM/key validation matching the CLI) as background jobs with stage progress, log tail, restart-surviving history (`data/web_jobs.json`), and dry-run previews served from memory. One run at a time (409 on conflict).
- **Discord → web linking** (#16). When `TUNEFINDER_WEB_BASE_URL` is set, report footers link to the web report ("Open in TuneFinder"), superseding the audition-page link.
- `tune_data()` — structured aggregation behind `tune-report` (text output byte-identical) for the web insights page.
- New runtime deps: `fastapi`, `uvicorn` (dev: `httpx`). Recorded assumption: authorised by the web-transformation brief.

## v0.10.0 — 2026-07-07

### Discovery quality (audit follow-up)

Work from the 2026-07-06 discovery audit (`docs/audit/2026-07-06-tunefinder-audit.md`, plan alongside it). Issues #1–#9, #11, #12; #9's flag stays off and #10 (label-roster fetcher) awaits its live spike.

- **Config-driven scoring weights** (#1). All ranker constants moved to a `scoring:` block in `config/settings.yaml`; defaults unchanged.
- **Two-axis scoring** (#2). Familiarity and discovery sub-scores accumulate alongside the total; Wildcards now ranks by discovery score with a familiarity ceiling — a genuine exploration channel instead of known-artist overflow. `scoring.wildcards_axis: combined` restores the old selection.
- **Genre affinity weights** (#3). `data/genre_affinity.json` (built from catalogue genre shares) scales `genre_match` per tag between `genre_affinity_min`/`_max`; missing data keeps flat behaviour. Run `build-profile` once to generate it.
- **Alias map + short-name guard** (#4). `config/aliases.yaml` resolves release aliases to canonical artists everywhere matching happens; artist matches under 4 characters need label/genre corroboration before claiming "You play X".
- **Label affinity memory** (#5). `data/label_affinity.json` persists artist↔label associations across runs (26-week freshness window) so Label Watch fires on quiet weeks; `tunefinder backfill-labels` seeds it from archived snapshots.
- **Scene one-hop signal** (#6). Unknown artists on labels your artists release on earn `scene_adjacent` (+0.75, discovery axis), with a mega-label roster guard.
- **Replay harness + tune-report** (#7). `tunefinder replay --week 2026-Wnn [--set path=value]` reconstructs a past report offline from the archive (window evaluated against that week) and diffs it against what was recommended; `tunefinder tune-report` turns feedback into per-signal/source/genre positive rates with lift and a thin-data caveat.
- **BPM/key-aware mix-prep** (#8). `mix-prep <genre> --bpm 170-180 [--key 8A] [--no-bpm-flex]` — Camelot-compatible key matching, half/double-time flex; unknown-metadata tracks are demoted, never dropped.
- **Remix-aware identity, flag off** (#9). `pipeline.remix_aware_identity: true` makes named remixes distinct works (owning the original no longer suppresses them) while generic versions keep merging; do not enable before the dry-run diff checklist on issue #9.
- **Taste recency + skip penalty** (#11). Play counts decay with an 18-month half-life via mix publish dates (`fetch_all_mixes` finally has its caller); artists with ≥2 unredeemed skips get a −1.0 penalty with a visible reason.
- **Robustness bundle** (#12). Catalog-API failure now falls back to last-saved profiles instead of killing the run; per-source share cap on weekly report slots (`scoring.max_share_per_source`); personal catalog URL moved out of code into `catalog.user_url`; Mixupload download counts wired as a small `source_popularity` signal; assorted doc drift fixed.

## v0.9.0 — 2026-06-12

### Audition queue & explain (Phase 2b)

Minor bump: audition HTML pages and the `explain` command. Full context: `docs/improvement-plan.md` §5 Phase 2.

- **Dedup metadata backfill across cross-source merges.** `_merge_group` in `src/pipeline/dedup.py` now backfills allowlisted embed/display keys (`beatport_id`, `volumo_track_id`, `volumo_album_id`, `bandcamp_album_id`, `bpm`, `key`, `keysign`) from merged-away duplicates. The winning item's values are never overwritten. Cross-source tracks — which score highest — now reliably carry ids from all constituent sources.
- **Embed metadata capture in fetchers.** `src/fetchers/bandcamp.py` now captures `bandcamp_album_id` from the `item_id` field of the `discover_web` response (confirmed numeric id, embed URL verified).
- **Audition page renderer.** `src/pipeline/audition.py` produces a fully self-contained HTML page (inline CSS + JS, no CDN). Player precedence per track: Bandcamp `EmbeddedPlayer` iframe (via `bandcamp_album_id`) → Beatport embed iframe (via `beatport_id`) → link-only row. Volumo has no preview URL in its API response — Volumo rows are link-only. The page header shows report_id and track count; each track shows number, artist/title, label, sources, BPM/key when present, the reason line (identical to Discord), a store link, and four copy-buttons for the `mark` command. Weekly pages copy `tunefinder mark {n} {outcome}`; mix-prep pages copy the shlex-quoted string form. The `data-cmd` attribute pattern keeps JS/HTML/shell quoting layers separate.
- **Audition pages written from runs.** `cmd_run` and `cmd_mix_prep` now call `write_audition_page` after each live run, writing to `data/reports/audition_{report_id}.html`. Dry-runs log "DRY RUN — audition page not written" and write nothing. The most recent 26 pages are retained (same policy as snapshot archives).
- **`tunefinder explain` command.** Traces any track through the weekly pipeline offline from current `data/` state. Prints fetched copies, dedup outcome, known-track/history/release-window verdict, scoring signals and total (with a single pass — `_score` is never called twice on the same object), section placement or skip reason, pool status, and feedback history. Works without Discord env vars. Output is labelled as a reconstruction — it can differ from the posted report if sources or the profile changed.

## v0.8.0 — 2026-06-12

### Feedback capture & ops hardening (Phase 2a)

Minor bump: two new CLI commands (`mark`, `stats`) and a backward-compatible extension to the history record schema. Full context: `docs/improvement-plan.md` §5 Phase 2.

- **Canonical report ordering (`report_order`).** `src/pipeline/report.py` now exports `report_order(sections)`, which returns candidates in exact rendered-number order — Label Watch expands with the same first-occurrence label grouping the renderer uses. `generate_report` is refactored to call `_group_label_watch` internally. Enables `mark <n>` track-number resolution.
- **History records now persist track number, signals, genres, and score.** `RecommendationRecord` gains `track_no`, `signal_codes`, `genre_tags`, `score`, and `label` (all optional, old files load with defaults). `history.py` serialisation extended symmetrically. `cmd_run` and `cmd_mix_prep` build records via `report_order` + `enumerate`; `all_section_candidates` deleted from `ranker.py`.
- **`tunefinder mark <selector> <outcome>` command.** Records an outcome (`bought` | `liked` | `skip` | `own`) against a recommended track. Selector is a track number (latest weekly report only) or `"Artist - Title"` (searches weekly then mix-prep history by dedup-key). Marks are append-only. Works without Discord env vars.
- **`tunefinder stats` command.** Aggregates `feedback.json` by signal code, source, genre, and report ID. Segmented weekly vs mix-prep; positive rate excludes `own`; `own` reported as identity-gap signal. Works without Discord env vars.
- **Bounded retry with jitter in `common.py`.** `get_html`/`post_html` now retry up to 3 times on `Timeout`, `ConnectionError`, and HTTP 5xx/429. Other 4xx (403/404) raise immediately. Backoffs: 2 s and 5 s + ≤0.5 s jitter. Fetcher POSTs are read-only search queries — retrying is safe.
- **Per-source anomaly alerts.** `src/pipeline/source_health.py` persists per-source fetch counts to `data/source_health.json` (rolling 26 entries). `cmd_run` detects errors, zero-count sources, and drops below 50 % of the trailing-4-run mean (configurable via `alerts.source_drop_threshold_pct` and `alerts.min_history_runs`). Anomalies post to the alert channel; dry-run logs only.
- **Dry-run alert leak fixed.** The no-candidates branches in `cmd_run` and `cmd_mix_prep` previously called `post_alert` unconditionally — a `--dry-run` with zero candidates would post a live Discord alert. Both are now guarded with `if not dry_run`.

## v0.7.1 — 2026-06-11

### Scoring hygiene (Phase 1.5)

Five scoring fixes that are wrong at any weight values — no weight re-tuning; that is Phase 2. Full rationale in `docs/scoring-review.md`.

- **`electronic` excluded from genre scoring.** `electronic` fires on nearly every track, making it a constant (+0.5) rather than a signal. It remains in `_BASELINE_GENRES` for genre-set augmentation and section cap exemption; it is excluded from the score calculation only.
- **`genre_match` capped at 2 tags.** Cross-source dedup unions genre tags, so a popular track could accumulate many tags and earn a large uncapped bonus. Cap at 2 prevents the same popularity fact being paid multiple times alongside `cross_source`.
- **`fresh_release` threshold tightened to 7 days.** The weekly corpus is date-filtered to ≤28 days, so the previous 30-day threshold fired on every dated track — a constant, not a signal. Tracks 8–28 days old still score normally on other signals.
- **Per-section score floor (`section_min_score: 1.0`).** Sections now skip candidates below the configured floor; thin weeks ship thin reports rather than filling slots with low-signal picks. Set to `0` to restore pre-v0.7.1 behaviour. Configurable in `config/settings.yaml`.
- **Mix-prep pool injection exempt from release-date window.** Weekly run already injected pool candidates without the window; mix-prep applied it inconsistently. Both modes are now consistent: the pool-age penalty handles staleness.

## v0.7.0 — 2026-06-11

### Report generation (deterministic — LLM stages removed)

- **LLM pipeline replaced with deterministic renderer.** `src/pipeline/reasons.py` composes one-sentence reasons from candidate data (artist play count, prior titles, chart position, cross-source count, label/artist facts, genre tags, days since release). `src/pipeline/report.py` renders the Discord-formatted report directly from those reasons — no HTTP calls, no JSON parse failures, no cascade exhaustion. Rationale and trade-offs: `docs/improvement-plan.md` §2.
- **Snapshot tests guard exact output.** Weekly and mix-prep report snapshots frozen in `tests/test_report.py`. Update them deliberately, never casually.
- **Required env vars reduced to Discord only.** `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, and `GEMINI_API_KEY` no longer required or used. `check-config` prints "Report generation: deterministic (no LLM)".
- **Continuous track numbering across all report sections.** Track numbers never reset between sections — enables the future `mark` command.
- **Label Watch shows artist-fact lines.** "*{n} of your artists release here: {names}*" derived from catalog data, never LLM recall.
- **Removed:** `src/llm.py`, `src/pipeline/label_cache.py`, `tests/test_llm.py`, `llm:` block in `config/settings.yaml`.

### Genre mapping cleanup

- **Volumo loose-fit mappings removed.** `uk-bass` (Bass House / Future House, id 2), `funk-soul-jazz` (Nu-Disco / Soul / Funk, id 17), and `hip-hop` (Soul / R&B / Hip-Hop, id 29) unmapped — Volumo has no true-fit genre for them. `downtempo` (Organic House / Downtempo, id 18) retained. See `docs/improvement-plan.md` §3.

### Sources

- **Juno deleted.** Site permanently shut down June 2026. Fetcher and config block removed; git history preserves the code.
- **Weekly source snapshots archived.** `archive_source_items()` in `src/fetchers/__init__.py` writes `data/archive/source_items_{report_id}.json.gz` after each run (both live and dry-run). Retention: 26 most-recent snapshots by mtime. Enables future offline replay/backtesting.

### Dead code removal

- `pipeline.max_candidates` config key and `Settings.pipeline_max_candidates` property removed (loaded but never consumed).
- `LabelRelevance` dataclass, `Candidate.is_known`, `Candidate.is_previously_recommended`, and `ArtistProfile.associated_labels` removed (all zero-usage).
- `sources.discogs` config block removed (no fetcher exists).
- `trafilatura` and `schedule` removed from `requirements.txt` (neither imported anywhere).
- Artist numbering-prefix guard added to `src/fetchers/catalog.py` — strips `^\d{1,3}[.)]\s+` from artist names at parse time to prevent dirty keys like `"15. zero t||sonic bionic"`.

## v0.6.6 — 2026-06-09

### Fixes

- **Volumo compilation album filtering.** Tracks on compilation albums were being tagged with the wrong genre when the album matched a genre query but individual tracks belonged to a different genre. For example, a Tech House track on a compilation with `album.genres: [2, 21]` would appear in `uk-bass` results because genre 2 (Bass House) was in the album's genre list. Fix: `_parse_track` now compares each track's `track.genre_id` against the queried genre ID set and drops mismatches. Tracks with no `genre_id` field are unchanged (no regression). `volumo_genre_id` added to `raw_metadata`. 5 tests added; pre-existing flaky assertion in `test_duplicate_genre_ids_deduplicated` fixed.

## v0.6.5 — 2026-06-06

### Sources

- **Volumo genre coverage extended.** Added `uk-bass` (Bass House / Future House, id 2), `funk-soul-jazz` (Nu-Disco / Soul / Funk, id 17), and `hip-hop` (Soul / R&B / Hip-Hop, id 29) — loose-fit mappings to the closest available Volumo genres, mirroring the tag coverage of other sources. README genre-coverage table updated to match.

## v0.6.4 — 2026-06-04

### Sources

- **Volumo enabled.** New source fetching curated new releases via the Volumo REST API (`/api/v1/albums`). Uses `sort=purchase` to avoid corrupted-date catalog entries. One request per internal TuneFinder tag with all Volumo genre IDs batched into a single call (e.g. all 8 house sub-genre IDs in one request). Pagination capped at 3 pages (150 albums) per tag. `curation: curated` restricts to Volumo-vetted releases only. `release_start_at` validated against a year-range guard (2020 ≤ year ≤ current+1) before use; falls back to album `first_live` timestamp if invalid; track skipped if both are invalid. Authentication via `VOLUMO_API_KEY` env var (optional — unauthenticated browsing returns full catalog data). Rich metadata captured: BPM, key signature, ISRC, catalog number, duration. 24 unit tests added.
- **Genres covered:** house (all 8 sub-genres), dnb, techno (raw + peak-time), breaks, ukg, electronica, downtempo.

## v0.6.3 — 2026-06-04

### Fixes

- **Bandcamp fetcher restored.** Bandcamp deprecated `hub/2/dig_deeper` (returned `{"error":true,"error_message":"bad function"}`). Migrated to `discover/1/discover_web` — the endpoint used by the new Vue SPA discover page. Payload now uses `category_id` (format filter), `tag_norm_names` (tag array), `slice`, `cursor`, and `include_result_types`. Response shape changed from `items[]` to `results[]`; artist extracted from `album_artist` or `band_name`; link comes directly from `item_url` (stripped of `?from=discover_page` suffix); `release_date` now populated from the response. All 13 configured tags fetch correctly (260 tracks per run).

## v0.6.2 — 2026-06-04

### Sources

- **Mixupload enabled.** Chart pages fetch via plain GET with `?date-month=MM.YYYY` parameter (not `?period=month` — that returns empty). Genre pages use `/genres/{slug}/page1` pagination. Artist name extracted from the second `<div>` inside `h3.for-sharing a` (the `.made a` uploader link is unreliable — can point to a label account). 19 tests added.
- **12 targets configured.** House (4 charts), techno, D&B, breaks, hip-hop, electronica, downtempo, UKG, and UK bass (`/genres/UKBass/page1`). Electronica and downtempo may return 0 tracks early in the month when charts are sparse.

## v0.6.1 — 2026-06-03

### Maintenance

- **Juno Download disabled.** The site shut down on 2026-06-01. Source toggled `enabled: false` in `config/settings.yaml`. All Juno tracks purged from `candidate_pool.json` (−295), `recommendation_history.json` (−15), and `source_items.json` (−900).
- **README updated.** Removed Juno from active sources table, genre feed table, tagline, and signal descriptions. Marked as disabled with shutdown note.

## v0.6.0

### Ranker

- **Catalog-augmented genre set.** The soft-match genre set is no longer a hardcoded 8-entry list. The curated baseline is unioned with any genre that appears across 3+ artist profiles in your catalog, so niche genres you actually play get matched.
- **Scaled label signal.** Replaced the flat `+2.5` label bonus with `1.5 + 0.5 × min(known_artists_on_label, 3)` (range 1.5–3.0). Labels with multiple of your artists on them score higher than labels with one.
- **Scaled cross-source signal.** Replaced the flat `+1.0` cross-source bonus with `0.5 × min(source_count, 4)` (range 1.0–2.0). A track flagged by 4 sources beats one flagged by 2.
- **Artist-recency penalty.** New `-0.75` penalty when any matched artist appeared in the recommendation history (weekly + mix-prep, combined) within the last 4 weeks. Rotates the report across your scene instead of repeatedly surfacing the same artists.
- **Pool age penalty.** New `-0.25` per week (cap `-1.5`) for candidates carried over from the persistent pool, based on `Candidate.pool_added_at`. Stale pool entries lose ground to fresh material. `max(0, ...)` clamp protects against future-dated timestamps.

### Stage 1 (reason enrichment)

- **Richer per-track payload.** The LLM now receives genre tags, chart position, cross-source count, your play count for known artists, and up to 3 of your prior tracks by that artist. Reasons can quote real catalog facts instead of paraphrasing signal text.
- **Tighter system prompt.** Explicit anchor list (prior track / genre / chart / cross-source count), `"Use only facts present in the payload"`, and banned marketing words (`sonic`, `undeniable`, `journey`, `vibes`, `must-hear`, `perfect for`, `your next favorite`).
- **Two-shot anchor.** Two input → output examples covering `known_artist + chart_position` and `label_match + genre_match`.
- **Per-call temperature override.** `call_stage1` now accepts an optional `temperature` parameter. Reason enrichment uses `0.3` for varied phrasing; label synopses keep the conservative default for factual grounding.

### Stage 2 (report writing)

- **Reasons surface in the output.** Stage 2 now renders `> {reason}` as a Discord blockquote line under each track. Previously reasons were computed and silently discarded.
- **This-week stats injected.** `generate_report` injects a one-line summary (totals / labels / known artists / top genres) into the user prompt. `generate_mix_prep_report` injects a slimmer variant (totals / top genres only) since the genre is already fixed by design.
- **Voice anti-patterns.** Same banned-word list as Stage 1, plus `"No filler intro before sections. No closing summary."`
- **Temperature `0.2 → 0.3`** in `config/settings.yaml`. Modest bump for prose breath.

### CLI

- **`--dry-run` actually skips Discord posts now.** Previously the help text claimed it did, but the code still called `discord.post_report` (just with a `🧪 [DRY RUN]` prefix). Both `cmd_run` and `cmd_mix_prep` now gate the Discord call behind `if not dry_run` and log a full report preview instead.

### Testing

- **pytest suite bootstrapped.** New `tests/` tree with 42 tests covering all new ranker, history, report, and LLM behavior. LLM HTTP calls mocked via `monkeypatch`. Run with `./venv/bin/pytest tests/ -v`. Dev dependencies live in `requirements-dev.txt`.

### Docs

- **`CLAUDE.md` relaxed.** Removed the "intentionally small and script-like" framing that discouraged tests and dev tooling. Dev dependencies in `requirements-dev.txt` no longer require ask-first.
- **`CHANGELOG.md` introduced.** This file. Historical `What's new` sections moved here from `README.md`.

### Maintenance

- Cleared `data/label_profiles.json` — the cached label synopses contained several factual hallucinations (Terrorhythm/Madrid, Text/Blawan, LuckyMe/London, Signature/progressive house, etc.). Cache will regenerate organically on the next run.

## v0.5.0

- **Pool candidates now respect the release date window in mix-prep.** Pool-injected candidates were bypassing `filter_release_date`, allowing stale tracks to appear in mix-prep results. Fixed.
- **UTC-aware date comparison in release date filter.** `date.today()` replaced with `datetime.now(UTC).date()` — avoids edge-case drift around midnight in non-UTC timezones.
- **Catalog base URL is now configurable.** `catalog_user_url` in `.env` is wired as the base URL for the catalog fetcher; `_DEFAULT_BASE_URL` remains as fallback.
- **Misc fixes.** Stale "Music Finder" brand name removed from fallback report header; duplicate label bracket removed from fallback label-watch lines; explicit `downtempo` tag mapping added to Bandcamp fetcher.

## v0.4.0

- **Concurrent mix-prep fetches.** Genre sources now fetch in parallel — mix-prep runs significantly faster, especially for wide genres like `house` that span 10+ feed endpoints across stores.
- **Configurable release date window.** New `pipeline.release_date_window_days` setting (default `28`) filters stale candidates before ranking. Juno's chart window slug derives from the same value. RA now populates `release_date` from review publication date so it benefits from the filter too.
- **Traxsource disabled by default.** The site now presents a Cloudflare challenge that makes unattended scraping unreliable. Can be re-enabled in `config/settings.yaml`.

## v0.3.0

- **Mistral/OpenRouter LLM setup.** Stage 1 (reason enrichment) uses Mistral Small as primary; Stage 2 (report writing) uses OpenRouter / DeepSeek as primary. Anthropic and Ollama providers removed from the cascade.
- **LLM fallback chains are configurable.** Both stages support explicit fallback chains in `config/settings.yaml`, though the default config has no active fallbacks.
- **Project renamed to TuneFinder.** Previously called MusicFinder.

## v0.2.0

- **Label synopsis in Label Watch.** Each label now gets a one-line header synopsis (founding city, year, key artists) written by Stage 1 LLM. Synopses are cached in `data/label_profiles.json` — the LLM is only called once per new label; repeat runs read from cache at zero cost.
- **Genre exclusion filter for mix-prep.** Tracks that pick up contradictory genre tags during cross-source dedup (e.g. a UKG track also tagged `electronica`) are filtered out of mix-prep results. Exclusion pairs are config-driven in `config/settings.yaml` so they can be tuned without code changes.
