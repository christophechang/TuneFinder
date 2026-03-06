# TuneFinder

Weekly music discovery automation for DJs. Monitors release feeds across multiple stores and platforms, scores new tracks against your personal mix history, and posts a curated report to a Discord channel — fully automated.

## How it works

1. **Profile** — pulls your published mix tracklist catalogue from the Changsta catalog API to build an artist taste profile and a known-track exclusion set
2. **Fetch** — scrapes new releases from Juno, Beatport, Bandcamp, Traxsource, and Resident Advisor
3. **Dedup** — normalises and deduplicates across sources, merging cross-source matches
4. **Rank** — scores candidates against your profile using weighted signals (known artist, recurring artist, label match, cross-source credibility, genre match, freshness)
5. **Report** — two-stage LLM pipeline: Stage 1 (Mistral) writes a one-line reason per track; Stage 2 (Claude Sonnet) writes the full Discord-formatted report
6. **Post** — sends the report to your Discord `#music-research` channel via Bot token

## Sources

| Source | Method | Status |
|---|---|---|
| Juno Download | RSS | ✅ |
| Beatport | `__NEXT_DATA__` JSON | ✅ |
| Bandcamp | `dig_deeper` JSON API | ✅ |
| Traxsource | HTML scrape | ✅ |
| Resident Advisor | `apolloState` JSON | ✅ |
| Boomkat | — | blocked (Cloudflare) |
| Bleep | — | requires login |

## Report sections

- **Top Picks** — highest overall score, any signal type
- **Label Watch** — releases on labels connected to artists you play
- **Artist Watch** — new material from artists already in your mixes
- **Wildcards** — interesting outliers from the remaining pool

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
ANTHROPIC_API_KEY=        # Stage 2 report generation
MISTRAL_API_KEY=          # Stage 1 reason enrichment (primary)
DISCORD_BOT_TOKEN=        # Discord bot token
DISCORD_GUILD_ID=         # Your Discord server ID

# Optional Stage 1 fallbacks (used if Mistral fails)
GROQ_API_KEY=
OPENROUTER_API_KEY=
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

1. Mistral `mistral-small-latest`
2. Groq `llama-3.1-70b-versatile`
3. Ollama `qwen3:8b` (local, configurable base URL)
4. OpenRouter `deepseek/deepseek-chat`

Stage 2 (report writing) always uses Anthropic Claude Sonnet.

## Project structure

```
src/
  config.py          # Settings loader (YAML + env vars)
  models.py          # Dataclasses — Track, Candidate, etc.
  llm.py             # Two-stage LLM cascade
  logger.py          # Structured logging setup
  fetchers/
    catalog.py       # Changsta catalog API (mix history + known tracks)
    juno.py          # Juno Download RSS
    beatport.py      # Beatport __NEXT_DATA__
    bandcamp.py      # Bandcamp dig_deeper API
    traxsource.py    # Traxsource HTML scrape
    ra.py            # Resident Advisor apolloState
    common.py        # Shared HTTP helpers
  pipeline/
    profile.py       # Artist profile builder
    dedup.py         # Normalisation and deduplication
    ranker.py        # Scoring and section assignment
    history.py       # Recommendation history store
    report.py        # LLM report generation
  output/
    discord.py       # Discord bot client
musicfinder/
  __main__.py        # CLI entry point
config/
  settings.yaml      # All non-secret configuration
```
