# TuneFinder

Weekly music discovery automation for DJs. Monitors release feeds across multiple stores and platforms, scores new tracks against your personal mix history, and posts a curated report to a Discord channel — fully automated.

> **Companion tool** — TuneFinder pairs with the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) to read your published mix tracklist history and build a personal taste profile. The profile drives all scoring — without it, artist and label signals won't fire.

## How it works

1. **Profile** — pulls your published mix tracklist catalogue from the [SoundCloud AI Mix Recommender API](https://github.com/christophechang/soundcloud-ai-mix-recommender-api) to build an artist taste profile and a known-track exclusion set
2. **Fetch** — scrapes new releases from Juno, Beatport, Bandcamp, Traxsource, Resident Advisor, and Subsurface Selections
3. **Dedup** — normalises and deduplicates across sources, merging cross-source matches
4. **Rank** — scores candidates against your profile using weighted signals (known artist, recurring artist, label match, cross-source credibility, genre match, freshness, chart position, source discovery bonus)
5. **Report** — two-stage LLM pipeline: Stage 1 runs a 6-provider cascade (Mistral → Groq → Gemini → MiniMax → OpenRouter → Anthropic) to write a one-line reason per track; Stage 2 (Claude Sonnet) writes the full Discord-formatted report
6. **Post** — sends the report to your Discord `#music-research` channel via Bot token

## Sources

| Source | Method | Status |
|---|---|---|
| Juno Download | Genre top-100 track chart | ✅ |
| Beatport | Genre top-100 chart (`__NEXT_DATA__` JSON) | ✅ |
| Bandcamp | `dig_deeper` JSON API | ✅ |
| Traxsource | HTML scrape | ✅ |
| Resident Advisor | `apolloState` JSON | ✅ |
| Subsurface Selections | Newsletter HTML scrape | ✅ |
| Boomkat | — | blocked (Cloudflare) |
| Bleep | — | requires login |

## Scoring signals

| Signal | Weight | Notes |
|---|---|---|
| `known_artist` | ×3.0 per play_count (max 10) | Artist appears in your mix history |
| `recurring_artist` | +2.0 | Artist has ≥3 mixes |
| `label_match` | +2.5 | Label has released known artists |
| `cross_source` | +1.0 | Track flagged by 2+ sources |
| `chart_position` | +0–1.5 | Linear decay from #1 (Juno/Beatport) |
| `human_curated` | +1.5 | Hand-picked by editorial source (Subsurface Selections) |
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
./venv/bin/python -m musicfinder mix-prep house
```

Valid genres: `dnb`, `breaks`, `house`, `techno`, `ukg`, `uk-bass`, `electronica`

The mix-prep report has two sections:
- **Top Picks** — highest-scored tracks for the genre
- **Deep Cuts** — next-tier selections worth exploring

Results are posted to the Discord `#mix-prep` channel. Mix-prep uses its own history file (`data/mix_prep_history.json`) so it won't deplete your weekly discovery feed — the same track can appear in both. Re-running mix-prep for the same genre will skip tracks already surfaced in prior mix-prep sessions.

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
ANTHROPIC_API_KEY=        # Stage 2 report generation (also Stage 1 last resort)
MISTRAL_API_KEY=          # Stage 1 primary
DISCORD_BOT_TOKEN=        # Discord bot token
DISCORD_GUILD_ID=         # Your Discord server ID

# Optional Stage 1 fallbacks (used in order if earlier providers fail)
GROQ_API_KEY=             # fallback 1 — free
GEMINI_API_KEY=           # fallback 2 — free
MINIMAX_API_KEY=          # fallback 3 — paid
OPENROUTER_API_KEY=       # fallback 4 — capped
```

## Commands

```bash
# Validate all required env vars and config
./venv/bin/python -m musicfinder check-config

# Save live API responses to fixtures/ for offline testing
./venv/bin/python -m musicfinder save-fixtures

# Build artist profiles and known-track exclusion set from mix history
./venv/bin/python -m musicfinder build-profile

# Fetch new releases from all enabled sources
./venv/bin/python -m musicfinder fetch-sources

# Run the full pipeline and post the weekly report to Discord
./venv/bin/python -m musicfinder run

# Generate a genre-focused track list for mix preparation
./venv/bin/python -m musicfinder mix-prep house
```

## First-time setup

```bash
./venv/bin/python -m musicfinder check-config
./venv/bin/python -m musicfinder save-fixtures
./venv/bin/python -m musicfinder build-profile
./venv/bin/python -m musicfinder run
```

## Configuration

Edit `config/settings.yaml` to:
- Adjust pipeline section counts (`top_picks_count`, `label_watch_count`, etc.)
- Enable/disable individual sources
- Change Discord channel names
- Swap LLM models

## LLM cascade

Stage 1 (reason enrichment) tries providers in order, skipping any with no API key set:

| # | Provider | Model | Cost |
|---|---|---|---|
| 1 | Mistral | `mistral-small-latest` | free (primary) |
| 2 | Groq | `llama-3.3-70b-versatile` | free |
| 3 | Gemini | `gemini-2.5-flash` | free |
| 4 | MiniMax | `MiniMax-M2.5` | paid |
| 5 | OpenRouter | `deepseek/deepseek-chat` | capped |
| 6 | Anthropic | `claude-sonnet-4-6` | capped (last resort) |

Stage 2 (report writing) always uses Anthropic Claude Sonnet directly — no fallback.

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
    traxsource.py    # Traxsource HTML scrape
    ra.py            # Resident Advisor apolloState
    subsurface.py    # Subsurface Selections newsletter scrape
    common.py        # Shared HTTP helpers
  pipeline/
    profile.py       # Artist profile builder
    dedup.py         # Normalisation and deduplication
    ranker.py        # Scoring and section assignment
    history.py       # Recommendation history store (weekly + mix-prep)
    pool.py          # Persistent candidate pool across runs
    report.py        # LLM report generation (weekly + mix-prep)
  output/
    discord.py       # Discord bot client
musicfinder/
  __main__.py        # CLI entry point
config/
  settings.yaml      # All non-secret configuration
```
