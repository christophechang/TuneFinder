# TuneFinder

Weekly music discovery automation for DJs. Monitors release feeds across multiple stores and platforms, scores new tracks against your personal mix history, and posts a curated report to a Discord channel — fully automated.

## How it works

1. **Profile** — pulls your published mix tracklist catalogue from the Changsta catalog API to build an artist taste profile and a known-track exclusion set
2. **Fetch** — scrapes new releases from Juno, Beatport, Bandcamp, Traxsource, and Resident Advisor
3. **Dedup** — normalises and deduplicates across sources, merging cross-source matches
4. **Rank** — scores candidates against your profile using weighted signals (known artist, recurring artist, label match, cross-source credibility, genre match, freshness)
5. **Report** — two-stage LLM pipeline: Stage 1 runs a 6-provider cascade (Mistral → Groq → Gemini → MiniMax → OpenRouter → Anthropic) to write a one-line reason per track; Stage 2 (Claude Sonnet) writes the full Discord-formatted report
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

To run the pipeline automatically every week, create a launchd plist. The example below runs every Sunday at 09:00.

**1. Create the plist**

Save to `~/Library/LaunchAgents/com.musicfinder.weekly.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.musicfinder.weekly</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USERNAME/Documents/Development/MusicFinder/venv/bin/python</string>
    <string>-m</string>
    <string>musicfinder</string>
    <string>run</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/YOUR_USERNAME/Documents/Development/MusicFinder</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>0</integer>  <!-- 0 = Sunday -->
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/YOUR_USERNAME/Documents/Development/MusicFinder/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USERNAME/Documents/Development/MusicFinder/logs/launchd.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

Replace `YOUR_USERNAME` with your macOS username (`whoami`).

**2. Load and enable**

```bash
launchctl load ~/Library/LaunchAgents/com.musicfinder.weekly.plist
```

**3. Useful commands**

```bash
# Check it loaded correctly
launchctl list | grep musicfinder

# Run immediately (for testing)
launchctl start com.musicfinder.weekly

# Unload / disable
launchctl unload ~/Library/LaunchAgents/com.musicfinder.weekly.plist

# Watch the log
tail -f /path/to/MusicFinder/logs/launchd.log
```

> **Note:** The Mac must be awake at the scheduled time. If it's asleep, launchd will run the job the next time it wakes. To change the day/time, edit `StartCalendarInterval` and reload the plist.

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
