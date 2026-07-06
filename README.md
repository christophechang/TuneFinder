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
3. **Dedup** — normalises and deduplicates across sources, merging cross-source matches; embed metadata (`beatport_id`, `bandcamp_album_id`, `bpm`, `keysign`, etc.) is backfilled from merged-away duplicates so cross-source tracks retain all embed ids
4. **Rank** — scores candidates against your profile using weighted signals (known artist, recurring artist, label match, cross-source credibility, genre match, freshness, chart position, source discovery bonus)
5. **Report** — deterministic renderer: reasons composed from catalog facts (play count, prior titles, chart position, label/artist data) in `src/pipeline/reasons.py`; Discord-formatted report built in `src/pipeline/report.py`
6. **Post** — sends the report to your Discord `#music-research` channel via Bot token

## Sources

| Source | Method | Status |
|---|---|---|
| Beatport | Genre top-100 chart (`__NEXT_DATA__` JSON) | ✅ |
| Bandcamp | `discover_web` JSON API | ✅ |
| Volumo | REST API (`/api/v1/albums`) | ✅ (no preview URLs in API — rows are link-only in audition page) |
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
| `scene_adjacent` | +0.75 | Unknown artist releasing on a label your known artists are on — "Label-mate of X on Label" (skipped for mega-labels; see below) |
| `cross_source` | +1.0 to +2.0 | Scales with source count (cap 4) — only credited when seen on 2+ |
| `chart_position` | +0–1.5 | Linear decay from #1 (Beatport, Traxsource, Mixupload when enabled) |
| `bandcamp_discovery` | +1.0 | Bandcamp — compensates for no chart data |
| `genre_match` | +0.5 per tag (cap 2), scaled ×0.5–×2.0 by genre affinity | Soft match against catalog-augmented genre set; `electronic` excluded (too broad); capped at 2 tags (highest-affinity tags counted first) to prevent cross-source tag inflation |
| `fresh_release` | +0.5 | Released within 7 days |
| `recent_recommendation` | −0.75 | Artist appeared in weekly or mix-prep history within last 4 weeks |
| `pool_age` | −0.25 per week (cap −1.5) | Carried over from the persistent pool — older entries lose ground |

Every candidate also gets two sub-totals alongside the combined score: **familiarity** (`known_artist`, `recurring_artist`, `recent_recommendation`) and **discovery** (`label_match`, `scene_adjacent`, `cross_source`, `genre_match`, `chart_position`, `fresh_release`, `bandcamp_discovery`). Top Picks, Label Watch, and Artist Watch still rank by the combined score — only Wildcards selection reads the discovery axis (see below). The `pool_age` penalty is deducted from the combined total only; both axes stay gross.

`genre_match` is scaled by genre affinity: `tunefinder build-profile` computes each genre's share of your mix catalogue (weighted by how often each track recurs) into `data/genre_affinity.json`, and every matching tag's contribution is multiplied by that share relative to your most-played genre, clamped to `scoring.genre_affinity_min`–`scoring.genre_affinity_max` (default 0.5–2.0). So a genre you play constantly scores near the max multiplier per tag, a genre you've barely touched scores near the floor, and a genre with no data at all (missing `genre_affinity.json`) falls back to a flat ×1.0 — today's behaviour.

`known_artist` matching resolves through `config/aliases.yaml` (release aliases → canonical mix-catalogue name — see Configuration below) before falling back to a direct name match. A matched artist-name part shorter than `scoring.min_artist_match_len` (default `4`) only counts toward `known_artist`/`recurring_artist` if the candidate also carries independent corroboration — a `label_match` or a `genre_match` on the same track. Uncorroborated short matches are dropped silently from scoring (logged at info level) rather than risk a false "You play X" claim from a short-name string collision.

### Label affinity memory

`label_match` used to be re-derived from scratch every run — a label only "existed" if one of your known artists released there *that week*. `data/label_affinity.json` (`src/pipeline/labels.py`) now persists artist↔label associations across runs, so a label you've connected to your taste in the past keeps informing Label Watch and `label_match` scoring even on a quiet week with no known-artist release on that label. Associations older than `scoring.label_memory_max_age_weeks` (default `26` weeks) are treated as stale and excluded. Every `run` and `mix-prep` reads the fresh memory before scoring and writes newly observed associations back after scoring (live runs only — `--dry-run` never touches the store). `tunefinder explain` reads the same memory for consistency but never writes it.

Use `tunefinder backfill-labels` to seed the store from your archived weekly fetches (`data/archive/source_items_*.json.gz`) — handy the first time you enable this, or after a gap in runs. It's read-only against your live state (no Discord, no history/pool writes) and idempotent — re-running it converges to the same store.

### Scene one-hop signal

`scene_adjacent` (issue #6) gives an artist you don't know at all a small, explainable nudge when they release on a label your known artists are on: "Label-mate of Calibre on Signature." It requires no `known_artist` match on the candidate at all (it's not needed — a known artist already scores via `known_artist`), and it deliberately stacks with `label_match` on the same track, since they're two modest signals about the same label fact rather than a double-charge for one. Guard against mega-labels: a label is only eligible if it has at most `scoring.scene_label_roster_cap` (default `30`) distinct artists in *that week's* candidate corpus — a label with hundreds of artists isn't a scene, it's a distributor. Set `scoring.w_scene_adjacent` to `0` to disable the signal entirely.

## Report sections

- **Top Picks** — highest overall score, any signal type
- **Label Watch** — releases on labels connected to artists you play
- **Artist Watch** — new material from artists already in your mixes
- **Wildcards** — genuine discovery channel: ranked by discovery score alone (not the combined score), and excludes anything with meaningful familiarity (`scoring.wildcards_max_familiarity`, default `0.0`) — no known-artist overflow. Set `scoring.wildcards_axis: combined` to restore the pre-v0.8 behaviour of ranking Wildcards by the combined score like the other sections.

Each track line includes a source tag (`[Beatport]`, `[Bandcamp]`, etc.) so you can see at a glance where each recommendation came from.

## Audition pages

Every live run (weekly and mix-prep) writes a self-contained HTML audition page to `data/reports/audition_{report_id}.html`. The page contains inline players where available, store links, and copy-buttons for the `mark` command.

**Player availability per source (Step 0 probed 2026-06-12):**

| Source | Player |
|---|---|
| Bandcamp | `EmbeddedPlayer` iframe — works (`item_id` field confirmed) |
| Beatport | embed iframe (`embed.beatport.com/?id={id}&type=track`) — works |
| Volumo | link-only — no preview URL field in the API response |
| Others | link-only |

- Pages are retained for the most recent 26 runs (same policy as source item archives).
- Dry-runs do not write pages — the run logs "DRY RUN — audition page not written".
- The page has no CDN dependencies. The only remote content is the store player iframes themselves, all `loading="lazy"`.
- Weekly pages copy `tunefinder mark {n} {outcome}` (number form resolves against the latest weekly report). Mix-prep pages copy the string form (`tunefinder mark "Artist - Title" {outcome}`).

## Explain

Trace any track through the weekly pipeline offline:

```bash
./venv/bin/python -m tunefinder explain "Calibre - New Dawn"
```

Output (example):

```
Reconstruction from current data/ state (source_items.json of the last fetch) — not a replay of the posted report.
Selector: 'Calibre - New Dawn'

Dedup key: 'calibre||new dawn'

=== FETCHED ===
  source='beatport' label='Signature' release_date='2026-06-10' genre_tags=[dnb] link='https://...'

=== DEDUP ===
  Merged item: seen_on_sources=['beatport', 'volumo'] genre_tags=[dnb]

=== KNOWN-TRACK FILTER ===
  PASS — not in known-track exclusion set.

=== HISTORY FILTER ===
  PASS — not in recommendation history.

=== RELEASE WINDOW ===
  PASS — release_date='2026-06-10' within 28-day window.

=== SCORING + SECTION RECONSTRUCTION ===
  Rank: #2 of 143 scored candidates (score=7.5)
  Signals:
    [known_artist] You play Calibre — this is new material from them.
    [recurring_artist] Calibre appears in 6 of your mixes.
    [cross_source] Flagged by 2 sources: Beatport, Volumo.

=== SECTION ===
  Landed in: top_picks (position #2)

=== POOL ===
  Not in pool.

=== FEEDBACK ===
  No feedback recorded.
```

`explain` works without Discord env vars (no `settings.validate()`). Output is labelled as a reconstruction — it can differ from the posted report if sources or the profile changed since the run.

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

Pool candidates injected into mix-prep are exempt from the release-date window (same as the weekly run). The pool-age penalty handles staleness; mix-prep benefits most from older pool gems.

### BPM/key filtering

`mix-prep` can narrow results to a tempo range and/or a harmonically compatible key — the facts a DJ actually filters by when building a set. BPM and key metadata come from `raw_metadata` (Volumo: `bpm` + `keysign`; Beatport: `bpm`; Mixupload: `bpm` + `key`) — coverage is partial across sources, so tracks with unknown BPM/key are **kept but demoted** below matching tracks, never dropped.

```bash
# Only 170-180 BPM dnb
./venv/bin/python -m tunefinder mix-prep dnb --bpm 170-180

# 170-180 BPM AND harmonically compatible with 8A (A minor)
./venv/bin/python -m tunefinder mix-prep dnb --bpm 170-180 --key 8A

# Musical notation also works for --key: Am, C major, F# minor, G#m...
./venv/bin/python -m tunefinder mix-prep house --key "C major"

# Disable half/double-time BPM matching (on by default — see below)
./venv/bin/python -m tunefinder mix-prep dnb --bpm 170-180 --no-bpm-flex
```

- `--bpm MIN-MAX` — numeric range (e.g. `170-180`); a track whose BPM is exactly double or half the range also matches by default (e.g. an 85 BPM track matches `170-180`) — pass `--no-bpm-flex` to require an exact in-range match.
- `--key CODE` — accepts Camelot notation (`8A`, `12B`) or musical key names (`Am`, `Abm`, `G#m`, `C major`, `F# minor`, unicode ♯/♭ all work); enharmonic equivalents (e.g. `G#m` / `Abm`) resolve to the same code. A track's key is considered compatible if it's an exact match, adjacent on the Camelot wheel (±1, wrapping 12↔1), or the same-numbered relative major/minor.
- Only tracks with a **known** BPM/key that actually *fails* a specified filter are dropped. Unknown values for an active filter never drop a track — they're kept and sorted below every matching track in both Top Picks and Deep Cuts.
- The report header shows which filters were active, and matched track lines show their BPM/key inline; with no `--bpm`/`--key`, the report is unchanged from before this feature.

## Genre coverage

Each internal genre maps to one or more genre feeds on each source. Sources not listed for a genre don't contribute to that genre's results.

| Genre | Beatport | Traxsource | Bandcamp | Mixupload | Volumo |
|---|---|---|---|---|---|
| `house` | house · melodic-house-techno · minimal-deep-tech · deep-house · tech-house | house · deep-house · soulful-house · tech-house · classic-house · minimal-deep-tech · nu-disco/indie-dance | house | style/house · style-part/deep-house · style-part/tech-house · style-part/progressive-house | house · deep-house · tech-house · soulful-house · funky-house · melodic-house-techno · progressive-house · afro-house |
| `dnb` | drum-bass | drum-and-bass | drum-and-bass | style/dnb | drum-and-bass |
| `breaks` | breaks-breakbeat-uk-bass ¹ | — | breakbeat | style/breaks | breaks-breakbeat |
| `uk-bass` | breaks-breakbeat-uk-bass ¹ | — | uk-bass | genres/UKBass/tracks | — |
| `ukg` | uk-garage-bassline | garage | uk-garage | style-part/uk-garage | uk-garage-2-step |
| `electronica` | electronica | electronica · leftfield | electronic · electronica | style-part/electronica | electronica |
| `downtempo` | downtempo | lounge-chill-out | downtempo · lounge | style-part/downtempo | organic-house-downtempo |
| `techno` | techno-raw-deep-hypnotic | techno | techno | style/techno | techno-raw-deep-dub · techno-peak-time |
| `funk-soul-jazz` | rb | soul-funk-disco | funk · r-b-soul | — | — |
| `hip-hop` | hip-hop | r-and-b-hip-hop | hip-hop-rap | style/hip-hop | — |

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
DISCORD_BOT_TOKEN=        # Discord bot token
DISCORD_GUILD_ID=         # Your Discord server ID

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

# Mix-prep narrowed by BPM range and Camelot-compatible key
./venv/bin/python -m tunefinder mix-prep dnb --bpm 170-180 --key 8A

# Record an outcome for a recommended track (no Discord env vars needed)
./venv/bin/python -m tunefinder mark 3 bought          # by track number (latest weekly report)
./venv/bin/python -m tunefinder mark "Calibre - New Dawn" liked   # by "Artist - Title"
./venv/bin/python -m tunefinder mark 7 own             # own = already had it (identity-gap miss)

# Show feedback statistics
./venv/bin/python -m tunefinder stats

# Trace a track through the weekly pipeline offline (no Discord env vars needed)
./venv/bin/python -m tunefinder explain "Calibre - New Dawn"

# Replay archived source_items snapshots into the label affinity store (see Label affinity memory)
./venv/bin/python -m tunefinder backfill-labels
```

### mark / stats notes

- `mark <n>` resolves against the **latest weekly report only**. Track numbers are stored from the first weekly run after v0.8.0 deploys — earlier reports have no stored numbers, use `"Artist - Title"` instead.
- `"Artist - Title"` searches weekly history first, then mix-prep history, matching by normalised dedup key (so `"Calibre - New Dawn"` finds a record stored as `"Calibre — New Dawn (Original Mix)"`).
- Outcomes: `bought` | `liked` | `skip` | `own`. `own` means "I already had this" — flagged as a known-track-filter miss, excluded from positive-rate calculations.
- Marks are append-only; `stats` uses the latest entry per (history, key).

## Configuration

Edit `config/settings.yaml` to:
- Adjust pipeline section counts (`top_picks_count`, `label_watch_count`, etc.)
- Set `pipeline.release_date_window_days` to control how far back the date filter looks (`7`, `28`, `56`, or `180` days)
- Set `pipeline.section_min_score` to require a minimum score before a track occupies a report slot (sections may run short on thin weeks; `0` disables the floor)
- Tune `pipeline.genre_exclusions` to drop tracks that pick up contradictory genre tags during cross-source dedup
- Enable/disable individual sources
- Change Discord channel names
- `alerts.source_drop_threshold_pct` (default `50`) — alert when a source's count falls below this % of its trailing-4-run average
- `alerts.min_history_runs` (default `2`) — prior runs required per source before drop detection activates (cold-start guard)
- **Scoring weights** — the `scoring:` block lets you tune all scoring constants (e.g. `w_known_artist`, `w_recurring`, `w_label_base`) without code changes. Omitted keys use defaults matching the weights listed in "Scoring signals" above.
- **Genre affinity** — `scoring.genre_affinity_min` / `scoring.genre_affinity_max` (default `0.5`/`2.0`) set the multiplier range `genre_match` is scaled by, derived from `data/genre_affinity.json` (rebuilt on every `build-profile`, `run`, and `mix-prep`). Delete the file to fall back to a flat ×1.0 multiplier.
- **Artist aliases** — `config/aliases.yaml` maps canonical mix-catalogue artist names to a list of release aliases they should also match: `canonical_name: [alias1, alias2]`. Matching is case-insensitive; a missing or empty file (the shipped default) simply disables alias resolution — no warning. A malformed file logs a warning and is treated as empty rather than crashing a run.
- **Short-name match guard** — `scoring.min_artist_match_len` (default `4`) prevents short artist-name parts (e.g. a 2–3 character alias or handle) from string-colliding with an unrelated release and producing a false "You play X" claim. A match shorter than this only counts if the candidate has independent corroboration (a label or genre match); otherwise it's dropped from scoring and logged.
- **Label affinity memory** — `scoring.label_memory_max_age_weeks` (default `26`) controls how long a persisted artist↔label association in `data/label_affinity.json` stays "fresh" enough to count toward Label Watch relevance and `label_match` scoring. See Label affinity memory above.

Traxsource note: the site is currently disabled by default in `config/settings.yaml` because it now presents a human verification checkbox/Cloudflare challenge that makes unattended scraping unreliable.

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
    profile.py         # Artist profile builder
    dedup.py           # Normalisation and deduplication
    ranker.py          # Scoring and section assignment
    history.py         # Recommendation history store (weekly + mix-prep)
    pool.py            # Persistent candidate pool across runs
    labels.py          # Persistent artist<->label affinity memory (data/label_affinity.json)
    harmonic.py        # BPM/key normalisation + Camelot compatibility (mix-prep --bpm/--key)
    reasons.py         # Deterministic reason composer
    report.py          # Deterministic report renderer (weekly + mix-prep)
    feedback.py        # Outcome marking and stats aggregation
    source_health.py   # Per-source run health persistence and anomaly detection
  output/
    discord.py       # Discord bot client
tunefinder/
  __main__.py        # CLI entry point
config/
  settings.yaml      # All non-secret configuration
data/
  recommendation_history.json   # Weekly recommendation records (gitignored)
  mix_prep_history.json         # Mix-prep recommendation records (gitignored)
  feedback.json                 # Outcome marks (append-only, gitignored)
  source_health.json            # Per-source run health for anomaly detection (gitignored)
  label_affinity.json           # Persisted artist<->label associations (gitignored)
```
