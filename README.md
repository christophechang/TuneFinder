# TuneFinder

[![GitHub release](https://img.shields.io/github/v/release/christophechang/TuneFinder)](https://github.com/christophechang/TuneFinder/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **Your crates, your taste.** Monitors new releases across five stores, scores them against your actual mix history, and posts a curated report to Discord — every week, fully automated.

Weekly music discovery automation for DJs. Monitors release feeds across multiple stores and platforms, scores new tracks against your personal mix history, and posts a curated report to a Discord channel — fully automated.

> **Companion tool** — TuneFinder pairs with the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) to read your published mix tracklist history and build a personal taste profile. The profile drives all scoring — without it, artist and label signals won't fire.

## What's new in v0.4.0

- **Concurrent mix-prep fetches.** Genre sources now fetch in parallel — mix-prep runs significantly faster, especially for wide genres like `house` that span 10+ feed endpoints across stores.
- **Configurable release date window.** New `pipeline.release_date_window_days` setting (default `28`) filters stale candidates before ranking. Juno's chart window slug derives from the same value. RA now populates `release_date` from review publication date so it benefits from the filter too.
- **Traxsource disabled by default.** The site now presents a Cloudflare challenge that makes unattended scraping unreliable. Can be re-enabled in `config/settings.yaml`.

## What's new in v0.3.0

- **MiniMax M2.7 as primary across both stages.** Both Stage 1 (reason enrichment) and Stage 2 (report writing) now use MiniMax M2.7 as primary. Anthropic and Ollama providers removed from the cascade.
- **Stage 2 fallback chain.** Stage 2 now has an explicit fallback chain (OpenRouter / DeepSeek) in `config/settings.yaml`, consistent with Stage 1.
- **Project renamed to TuneFinder.** Previously called MusicFinder.

## What's new in v0.2.0

- **Label synopsis in Label Watch.** Each label now gets a one-line header synopsis (founding city, year, key artists) written by Stage 1 LLM. Synopses are cached in `data/label_profiles.json` — the LLM is only called once per new label; repeat runs read from cache at zero cost.
- **Genre exclusion filter for mix-prep.** Tracks that pick up contradictory genre tags during cross-source dedup (e.g. a UKG track also tagged `electronica`) are filtered out of mix-prep results. Exclusion pairs are config-driven in `config/settings.yaml` so they can be tuned without code changes.

## How it works

1. **Profile** — pulls your published mix tracklist catalogue from the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) to build an artist taste profile and a known-track exclusion set
2. **Fetch** — scrapes new releases from Juno, Beatport, and Bandcamp (Traxsource and Resident Advisor are available but disabled by default)
3. **Dedup** — normalises and deduplicates across sources, merging cross-source matches
4. **Rank** — scores candidates against your profile using weighted signals (known artist, recurring artist, label match, cross-source credibility, genre match, freshness, chart position, source discovery bonus)
5. **Report** — two-stage LLM pipeline: Stage 1 runs a 4-provider cascade (MiniMax → Mistral Small → Groq → Gemini) to write a one-line reason per track; Stage 2 (MiniMax, fallback OpenRouter/DeepSeek) writes the full Discord-formatted report
6. **Post** — sends the report to your Discord `#music-research` channel via Bot token

## Sources

| Source | Method | Status |
|---|---|---|
| Juno Download | Genre top-100 track chart | ✅ |
| Beatport | Genre top-100 chart (`__NEXT_DATA__` JSON) | ✅ |
| Bandcamp | `dig_deeper` JSON API | ✅ |
| Traxsource | HTML scrape | disabled (human verification challenge) |
| Resident Advisor | `apolloState` JSON | disabled by default |
| Boomkat | — | blocked (Cloudflare) |
| Bleep | — | requires login |

## Scoring signals

| Signal | Weight | Notes |
|---|---|---|
| `known_artist` | ×3.0 per play_count (max 10) | Artist appears in your mix history |
| `recurring_artist` | +2.0 | Artist has ≥3 mixes |
| `label_match` | +2.5 | Label has released known artists |
| `cross_source` | +1.0 | Track flagged by 2+ sources |
| `chart_position` | +0–1.5 | Linear decay from #1 (Juno/Beatport/Traxsource) |
| `bandcamp_discovery` | +1.0 | Bandcamp — compensates for no chart data |
| `genre_match` | +0.5 per tag | Soft match against DJ's genre set |
| `fresh_release` | +0.5 | Released within 30 days |

## Report sections

- **Top Picks** — highest overall score, any signal type
- **Label Watch** — releases on labels connected to artists you play
- **Artist Watch** — new material from artists already in your mixes
- **Wildcards** — interesting outliers from the remaining pool

Each track line includes a source tag (`[Juno]`, `[Beatport]`, `[Bandcamp]`, etc.) so you can see at a glance where each recommendation came from.

## Mix prep

When preparing a mix in a specific style, run `mix-prep <genre>` to get a focused report of the best available tracks for that genre:

```bash
./venv/bin/python -m tunefinder mix-prep house
```

Valid genres: `dnb` · `breaks` · `house` · `ukg` · `uk-bass` · `electronica` · `downtempo` · `techno` · `funk-soul-jazz` · `hip-hop`

The mix-prep report has two sections:
- **Top Picks** — highest-scored tracks for the genre
- **Deep Cuts** — next-tier selections worth exploring

Results are posted to the Discord `#mix-prep` channel. Mix-prep uses its own history file (`data/mix_prep_history.json`) so it won't deplete your weekly discovery feed — the same track can appear in both. Re-running mix-prep for the same genre will skip tracks already surfaced in prior mix-prep sessions.

## Genre coverage

Each internal genre maps to one or more genre feeds on each source. Sources not listed for a genre don't contribute to that genre's results.

| Genre | Juno | Beatport | Traxsource | Bandcamp |
|---|---|---|---|---|
| `house` | house | house · melodic-house-techno · minimal-deep-tech · deep-house · tech-house | house · deep-house · soulful-house · tech-house · classic-house · minimal-deep-tech · nu-disco/indie-dance | house |
| `dnb` | drumandbass | drum-bass | drum-and-bass | drum-and-bass |
| `breaks` | breakbeat | breaks-breakbeat-uk-bass ¹ | — | breakbeat |
| `uk-bass` | bass | breaks-breakbeat-uk-bass ¹ | — | uk-bass |
| `ukg` | 4x4-garage | uk-garage-bassline | garage | uk-garage |
| `electronica` | leftfield | electronica | electronica · leftfield | electronic · electronica |
| `downtempo` | downtempo | downtempo | lounge-chill-out | downtempo · lounge |
| `techno` | — | techno-raw-deep-hypnotic | techno | techno |
| `funk-soul-jazz` | funk-soul-jazz | rb | soul-funk-disco | funk · r-b-soul |
| `hip-hop` | hip-hop | hip-hop | r-and-b-hip-hop | hip-hop-rap |

¹ Beatport's breaks and uk-bass share a single combined feed. Per-track genre slugs from the page data are used to split them into the correct internal tags.

## Setup

**Requirements:** Python 3.11+

```bash
git clone git@github.com:christophechang/TuneFinder.git
cd TuneFinder
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
# fill in .env with your API keys
```

## Environment variables

```
# Required
MINIMAX_API_KEY=          # Stage 1 + Stage 2 primary
DISCORD_BOT_TOKEN=        # Discord bot token
DISCORD_GUILD_ID=         # Your Discord server ID

# Optional fallbacks (used in order if primary fails)
MISTRAL_API_KEY=          # Stage 1 fallback 1
GROQ_API_KEY=             # Stage 1 fallback 2 — free
GEMINI_API_KEY=           # Stage 1 fallback 3 — free
OPENROUTER_API_KEY=       # Stage 2 fallback 1 — capped
```

## Commands

```bash
# Validate all required env vars and config
./venv/bin/python -m tunefinder check-config

# Save live API responses to fixtures/ for offline testing
./venv/bin/python -m tunefinder save-fixtures

# Build artist profiles and known-track exclusion set from mix history
./venv/bin/python -m tunefinder build-profile

# Fetch new releases from all enabled sources
./venv/bin/python -m tunefinder fetch-sources

# Run the full pipeline and post the weekly report to Discord
./venv/bin/python -m tunefinder run

# Dry-run (full pipeline, no Discord posts or history writes)
./venv/bin/python -m tunefinder run --dry-run

# Generate a genre-focused track list for mix preparation
./venv/bin/python -m tunefinder mix-prep house
```

## First-time setup

```bash
./venv/bin/python -m tunefinder check-config
./venv/bin/python -m tunefinder save-fixtures
./venv/bin/python -m tunefinder build-profile
./venv/bin/python -m tunefinder run
```

## Configuration

Edit `config/settings.yaml` to:
- Adjust pipeline section counts (`top_picks_count`, `label_watch_count`, etc.)
- Set `pipeline.release_date_window_days` to control how far back the date filter looks (must be `7`, `28`, `56`, or `180` — maps to Juno chart windows)
- Tune `pipeline.genre_exclusions` to drop tracks that pick up contradictory genre tags during cross-source dedup
- Enable/disable individual sources
- Change Discord channel names
- Swap LLM models

Traxsource note: the site is currently disabled by default in `config/settings.yaml` because it now presents a human verification checkbox/Cloudflare challenge that makes unattended scraping unreliable.

## LLM cascade

Both stages try providers in order, skipping any with no API key set.

**Stage 1** (reason enrichment + label synopses):

| # | Provider | Model | Cost |
|---|---|---|---|
| 1 | MiniMax | `MiniMax-M2.7` | paid (primary) |
| 2 | Mistral | `mistral-small-latest` | paid |
| 3 | Groq | `llama-3.3-70b-versatile` | free |
| 4 | Gemini | `gemini-2.5-flash` | free |

**Stage 2** (report writing):

| # | Provider | Model | Cost |
|---|---|---|---|
| 1 | MiniMax | `MiniMax-M2.7` | paid (primary) |
| 2 | OpenRouter | `deepseek/deepseek-chat` | capped |

## Scheduling (macOS launchd)

Runs every Sunday at 09:00. Logs to `logs/launchd.log`.

```bash
# Edit plist to set YOUR_ADMIN_USER and venv path
nano com.openclaw.tune-finder.plist

# Install
cp com.openclaw.tune-finder.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.tune-finder.plist

# Verify
launchctl list | grep tune-finder

# Test trigger
launchctl start com.openclaw.tune-finder
```

## Project structure

```
src/
  config.py          # Settings loader (YAML + env vars)
  models.py          # Dataclasses — Track, Candidate, etc.
  llm.py             # Two-stage LLM cascade
  logger.py          # Structured logging setup
  fetchers/
    catalog.py       # SoundCloud AI Mix Recommender API (mix history + known tracks)
    juno.py          # Juno Download genre top-100 chart
    beatport.py      # Beatport genre top-100 chart (__NEXT_DATA__)
    bandcamp.py      # Bandcamp dig_deeper API
    traxsource.py    # Traxsource HTML scrape (currently disabled by default)
    ra.py            # Resident Advisor apolloState
    boomkat.py       # Boomkat (disabled — Cloudflare bot protection)
    bleep.py         # Bleep (disabled — requires login)
    common.py        # Shared HTTP helpers
  pipeline/
    profile.py       # Artist profile builder
    dedup.py         # Normalisation and deduplication
    ranker.py        # Scoring and section assignment
    history.py       # Recommendation history store (weekly + mix-prep)
    pool.py          # Persistent candidate pool across runs
    label_cache.py   # Persistent label synopsis cache
    report.py        # LLM report generation (weekly + mix-prep)
  output/
    discord.py       # Discord bot client
tunefinder/
  __main__.py        # CLI entry point
config/
  settings.yaml      # All non-secret configuration
```
