# TuneFinder

[![GitHub release](https://img.shields.io/github/v/release/christophechang/TuneFinder)](https://github.com/christophechang/TuneFinder/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **Your crates, your taste.** Monitors new releases across Beatport, Bandcamp, Volumo, and Mixupload, scores them against your actual mix history, and posts a curated report to Discord — every week, fully automated.

> **Companion tool** — TuneFinder pairs with the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) to read your published mix tracklist history and build a personal taste profile. The profile drives all scoring — without it, artist and label signals won't fire.

This project explores AI-assisted development workflows. My focus here was system design and delivery rather than idiomatic Python, which is not my primary stack.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full release history.

## How it works

1. **Profile** — pulls your published mix tracklist catalogue from the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) to build an artist taste profile and a known-track exclusion set
2. **Fetch** — scrapes new releases from Beatport, Bandcamp, Volumo, and Mixupload (Traxsource and Resident Advisor are available but disabled by default)
3. **Dedup** — normalises and deduplicates across sources, merging cross-source matches
4. **Rank** — scores candidates against your profile using weighted signals (known artist, recurring artist, label match, cross-source credibility, genre match, freshness, chart position, source discovery bonus)
5. **Report** — two-stage LLM pipeline: Stage 1 uses Mistral Small to write a one-line reason per track; Stage 2 uses OpenRouter / DeepSeek to write the full Discord-formatted report
6. **Post** — sends the report to your Discord `#music-research` channel via Bot token

## Sources

| Source | Method | Status |
|---|---|---|
| Beatport | Genre top-100 chart (`__NEXT_DATA__` JSON) | ✅ |
| Bandcamp | `discover_web` JSON API | ✅ |
| Volumo | REST API (`/api/v1/albums`) | ✅ |
| Mixupload | HTML scrape (chart + genre pages) | ✅ |
| Traxsource | HTML scrape | disabled (human verification challenge) |
| Resident Advisor | `apolloState` JSON | disabled by default |
| Boomkat | — | blocked (Cloudflare) |
| Bleep | — | requires login |

## Scoring signals

| Signal | Weight | Notes |
|---|---|---|
| `known_artist` | ×3.0 per play_count (max 10) | Artist appears in your mix history |
| `recurring_artist` | +2.0 | Artist has ≥3 mixes |
| `label_match` | +1.5 to +3.0 | Scales with how many of your known artists appear on the label (cap 3) |
| `cross_source` | +1.0 to +2.0 | Scales with source count (cap 4) — only credited when seen on 2+ |
| `chart_position` | +0–1.5 | Linear decay from #1 (Beatport, Traxsource, Mixupload when enabled) |
| `bandcamp_discovery` | +1.0 | Bandcamp — compensates for no chart data |
| `genre_match` | +0.5 per tag | Soft match against catalog-augmented genre set |
| `fresh_release` | +0.5 | Released within 30 days |
| `recent_recommendation` | −0.75 | Artist appeared in weekly or mix-prep history within last 4 weeks |
| `pool_age` | −0.25 per week (cap −1.5) | Carried over from the persistent pool — older entries lose ground |

## Report sections

- **Top Picks** — highest overall score, any signal type
- **Label Watch** — releases on labels connected to artists you play
- **Artist Watch** — new material from artists already in your mixes
- **Wildcards** — interesting outliers from the remaining pool

Each track line includes a source tag (`[Beatport]`, `[Bandcamp]`, etc.) so you can see at a glance where each recommendation came from.

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

| Genre | Beatport | Traxsource | Bandcamp | Mixupload | Volumo |
|---|---|---|---|---|---|
| `house` | house · melodic-house-techno · minimal-deep-tech · deep-house · tech-house | house · deep-house · soulful-house · tech-house · classic-house · minimal-deep-tech · nu-disco/indie-dance | house | style/house · style-part/deep-house · style-part/tech-house · style-part/progressive-house | house · deep-house · tech-house · soulful-house · funky-house · melodic-house-techno · progressive-house · afro-house |
| `dnb` | drum-bass | drum-and-bass | drum-and-bass | style/dnb | drum-and-bass |
| `breaks` | breaks-breakbeat-uk-bass ¹ | — | breakbeat | style/breaks | breaks-breakbeat |
| `uk-bass` | breaks-breakbeat-uk-bass ¹ | — | uk-bass | genres/UKBass/tracks | bass-house-future-house ² |
| `ukg` | uk-garage-bassline | garage | uk-garage | style-part/uk-garage | uk-garage-2-step |
| `electronica` | electronica | electronica · leftfield | electronic · electronica | style-part/electronica | electronica |
| `downtempo` | downtempo | lounge-chill-out | downtempo · lounge | style-part/downtempo | organic-house-downtempo |
| `techno` | techno-raw-deep-hypnotic | techno | techno | style/techno | techno-raw-deep-dub · techno-peak-time |
| `funk-soul-jazz` | rb | soul-funk-disco | funk · r-b-soul | — | nu-disco-soul-funk ² |
| `hip-hop` | hip-hop | r-and-b-hip-hop | hip-hop-rap | style/hip-hop | soul-rb-hip-hop ² |

¹ Beatport's breaks and uk-bass share a single combined feed. Per-track genre slugs from the page data are used to split them into the correct internal tags.

² Loose-fit mapping — closest available Volumo genre to the internal tag, not an exact match.

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
MISTRAL_API_KEY=          # Stage 1 primary
OPENROUTER_API_KEY=       # Stage 2 primary
DISCORD_BOT_TOKEN=        # Discord bot token
DISCORD_GUILD_ID=         # Your Discord server ID

# Optional fallbacks
GROQ_API_KEY=             # Stage 1 fallback 1
GEMINI_API_KEY=           # Stage 1 fallback 2

# Sources (optional)
VOLUMO_API_KEY=           # Volumo — unauthenticated browsing works without this
```

## First-time setup

```bash
./venv/bin/python -m tunefinder check-config
./venv/bin/python -m tunefinder save-fixtures
./venv/bin/python -m tunefinder build-profile
./venv/bin/python -m tunefinder run
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

# Dry-run mix-prep (full pipeline, no Discord posts or history writes)
./venv/bin/python -m tunefinder mix-prep house --dry-run
```

## Configuration

Edit `config/settings.yaml` to:
- Adjust pipeline section counts (`top_picks_count`, `label_watch_count`, etc.)
- Set `pipeline.release_date_window_days` to control how far back the date filter looks (`7`, `28`, `56`, or `180` days)
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
| 1 | Mistral | `mistral-small-latest` | paid (primary) |
| 2 | Groq | `llama-3.3-70b-versatile` | free |
| 3 | Gemini | `gemini-2.5-flash` | free |

**Stage 2** (report writing):

| # | Provider | Model | Cost |
|---|---|---|---|
| 1 | OpenRouter | `deepseek/deepseek-chat` | capped (primary) |

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
    beatport.py      # Beatport genre top-100 chart (__NEXT_DATA__)
    bandcamp.py      # Bandcamp discover_web API
    volumo.py        # Volumo REST API (/api/v1/albums)
    mixupload.py     # Mixupload HTML scrape (chart + genre pages)
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
