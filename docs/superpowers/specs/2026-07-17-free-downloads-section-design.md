# Free Downloads report section — design

**Date:** 17 July 2026
**Status:** approved by Christophe (routing exclusivity and mix-prep scope chosen explicitly)
**Context:** v0.14.0 added the SoundCloud free-download/bootleg source. The lane fetches (~117
items/run verified in prod) but nothing places: bootlegs structurally lack every signal the ranker
pays for (no charts, labels, or cross-source presence), score ~0.25–1.0, and die at the
`section_min_score: 1.0` floor — in weekly *and* mix-prep. Decision: free-DL tracks get their own
report section and never compete with store releases ("apples and oranges"), in both report kinds.

## Decisions (owner's calls)

1. **Exclusive routing.** A free-DL track appears only in the Free Downloads section — even when
   its artist is known. Known-artist items rank top of the section instead (score-ordered), and the
   reason line still says "You play X". No track ever appears in two sections.
2. **Both report kinds.** Weekly gets the section after Wildcards; mix-prep gets it after Deep
   Cuts (genre-filtered like the rest of mix-prep).

## Routing mechanism (approach A)

Partition at section-assignment time, config-driven:

- New `pipeline.free_download_sources: [soundcloud]` in `settings.yaml`. Candidates whose
  **primary** `source` is in the list are pulled out of the assignment pool before Top Picks /
  Label Watch / Artist Watch / Wildcards run, and are the only candidates eligible for the Free
  Downloads section. Hypeddit (issue #19) joins later by adding one string.
- Accepted edge: a bootleg that cross-source-merged with a store listing routes by its primary
  source. Rare by nature of the lane; documented, not special-cased.
- Unplaced lane items flow into the candidate pool exactly as store tracks do (pool-age penalty,
  500 cap, re-compete next run).

## Config

```yaml
pipeline:
  free_download_sources: [soundcloud]
  free_downloads_count: 5             # weekly slots
  mix_prep_free_downloads_count: 10   # mix-prep slots
  free_downloads_min_score: 0.0       # lane floor — 0 = best N of what the lane found
scoring:
  w_soundcloud_popularity: 0.25       # bonus when download_count >= threshold (discovery axis)
  soundcloud_popularity_downloads: 50 # min download_count to earn the signal
```

The main `section_min_score` (1.0) deliberately does **not** apply to the lane: it is calibrated to
store signals the lane cannot have. `free_downloads_min_score` exists as its own knob, default 0 —
a thin fetch ships a thin section, matching house philosophy.

## Ranker (`src/pipeline/ranker.py`)

- **New signal** `soundcloud_popularity`: `+w_soundcloud_popularity` (score + discovery axis) when
  `source == "soundcloud"` and `raw_metadata["download_count"] >= soundcloud_popularity_downloads`.
  Exact structural mirror of the Mixupload popularity signal.
- **`assign_sections` (weekly):** partition lane candidates first; assign Free Downloads by
  combined score (desc, existing stable tiebreak), apply `free_downloads_min_score`, cap at
  `free_downloads_count`. Store candidates proceed through the existing assignment untouched. Lane
  candidates are excluded from Wildcards eligibility entirely.
- **Mix-prep assignment:** same partition; Free Downloads block capped at
  `mix_prep_free_downloads_count`, same lane floor. Lane items no longer compete for Top Picks /
  Deep Cuts there.
- Rejection tracing (`explain`) gets lane-aware entries ("routed to free_downloads",
  "below lane floor") so `tunefinder explain` stays truthful.

## Report rendering (`src/pipeline/report.py`)

- `_SECTION_ORDER` becomes `("top_picks", "label_watch", "artist_watch", "wildcards",
  "deep_cuts", "free_downloads")` — one tuple serves both kinds: absent keys skip, so weekly shows
  it after Wildcards and mix-prep after Deep Cuts. (Verified: unknown keys raise `ValueError` at
  `report.py:49`; empty sections are skipped.)
- Header: `## 🆓 Free Downloads`, existing two-line track format, no layout changes otherwise.
- Continuous track numbering flows through the section — `tunefinder mark <n>` and web feedback
  address these tracks like any other.
- Snapshot tests updated deliberately in the same commit as the renderer change.

## Reasons (`src/pipeline/reasons.py`)

- New fact: `download_count`. The `soundcloud_popularity` signal gets 2–3 deterministic template
  variants (stable-hash selection, per house style), e.g.
  "Free DL — grabbed 214 times on SoundCloud."
- Precedence: slotted at the `bandcamp_discovery` tier (source-signal level), below
  known_artist/label/chart/cross-source.

## Artifact, audition page, web

- `report_artifact.py` `_SECTION_LABELS` and `audition.py` `_SECTION_LABELS` gain
  `free_downloads: "Free Downloads"`.
- `ReportSection.key` is a free `str` in the OpenAPI schema (verified `schemas.py:68`) — **no
  schema change, no tunefinder-web type regen, no web release**. The SPA renders sections
  generically; SoundCloud rows already carry the widget player and the "Downloaded" button
  (tunefinder-web v0.6.0). Record Bag is outcome-driven and needs nothing.
- History: `RecommendationRecord` rows are written for the section like any other (weekly and
  mix-prep histories), preventing repeats. No schema change.

## Explicitly out of scope

Dedup identity, pool mechanics, history schema, fetcher behavior, Discord posting mechanics, any
change to how store sections rank. No `w_soundcloud` flat source bonus — the dedicated section
removes the need that bonus was compensating for.

## Tests

- **Ranker:** lane partition/exclusivity (high-scoring lane item never enters main sections or
  Wildcards; store item never enters Free Downloads), known-artist lane item ranks first in-lane,
  lane floor + count caps, popularity signal fires only at/above threshold and only for soundcloud,
  mix-prep block equivalents, empty-lane behavior.
- **Reasons:** matrix rows for the new signal's variants.
- **Report:** weekly + mix-prep snapshots updated deliberately; numbering continuity across the
  new section.
- **Artifact/web API:** section present with label; `test_web_api` report-detail passes unchanged
  (free-string key).
- **Config:** new keys' defaults and plumbing.

## Rollout

Feature branch off `develop` → PR → merge → release (next minor, v0.15.0) via the standard
deploy-release runbook. Backend-only: no tunefinder-web release required. Validation before PR:
full pytest, `check-config`, and a `run --dry-run` + `mix-prep <genre> --dry-run` pasted for review
showing the section populated.
