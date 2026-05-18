# Changelog

All notable changes to TuneFinder. The format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
