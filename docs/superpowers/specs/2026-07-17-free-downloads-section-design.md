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

- **Bug fix — source-gate the existing Mixupload popularity signal.** The current condition
  (`ranker.py:498-507`) fires on `download_count` alone, with no source check, and hardcodes
  "downloads on Mixupload" in the explanation. Since v0.14.0, SoundCloud items with ≥100 downloads
  already receive a spurious +0.25 and a false Mixupload reason (masked in reports by the floor,
  but polluting pool scores). Gate it on `c.source == "mixupload"`, with a regression test proving
  it never fires for SoundCloud.
- **New signal** `soundcloud_popularity`: `+w_soundcloud_popularity` (score + discovery axis) when
  `source == "soundcloud"` and `raw_metadata["download_count"] >= soundcloud_popularity_downloads`.
  Exact structural mirror of the (now source-gated) Mixupload popularity signal; a track can never
  earn both.
- **`assign_sections` (weekly):** partition lane candidates first; assign Free Downloads by
  combined score (desc, existing stable tiebreak), apply `free_downloads_min_score`, cap at
  `free_downloads_count`. Store candidates proceed through the existing assignment untouched. Lane
  candidates are excluded from Wildcards eligibility entirely.
- **Mix-prep assignment:** same partition; Free Downloads block capped at
  `mix_prep_free_downloads_count`, same lane floor. Lane items no longer compete for Top Picks /
  Deep Cuts there.
- **Harmonic-filter interplay (mix-prep):** when `--bpm`/`--key` filters are active, the existing
  rule places harmonic matches ahead of demoted unknowns regardless of score (`ranker.py:770`).
  The Free Downloads block preserves that ordering internally (today all SoundCloud items are
  BPM-less unknowns; future lane sources may carry BPM), and demotion must never *exclude* a lane
  item from the block. The intersection is tested explicitly.
- Rejection tracing (`explain`) gets lane-aware entries ("routed to free_downloads",
  "below lane floor") so `tunefinder explain` stays truthful.

## Report rendering — all four paths, enumerated

Section rendering is **not** driven solely by `_SECTION_ORDER`: the Discord renderers contain
manual per-section blocks and the artifact builder iterates its own literal tuple. Adding the
section therefore touches every path explicitly:

1. `report.py` `_SECTION_ORDER` becomes `("top_picks", "label_watch", "artist_watch",
   "wildcards", "deep_cuts", "free_downloads")` — absent keys skip, so weekly shows it after
   Wildcards and mix-prep after Deep Cuts. (Unknown keys raise `ValueError` at `report.py:49`;
   empty sections are skipped.)
2. `report.py` **weekly renderer**: new manual block after Wildcards (`## 🆓 Free Downloads`,
   existing two-line track format), mirroring `report.py:302-340`.
3. `report.py` **mix-prep renderer**: new manual block after Deep Cuts, same format.
4. `report_artifact.py`: its literal section tuple (`report_artifact.py:114`) is replaced by
   importing the shared `_SECTION_ORDER` from `report.py` (targeted unification — audition.py
   already imports it), plus `free_downloads: "Free Downloads"` in `_SECTION_LABELS`.
   `audition.py` `_SECTION_LABELS` gains the same entry (its `.title()` fallback would cover it,
   but explicit beats implicit).

A cross-renderer consistency test asserts a candidate placed in `free_downloads` appears in the
Discord text, the artifact, and the audition page for the same sections dict — so a future section
can't silently miss a path.

**Numbering and marking (scoped precisely):** continuous track numbering flows through the section
in both report kinds. Numeric CLI selectors (`tunefinder mark <n>`) resolve against **weekly**
history only — existing behaviour (`feedback.py:101`), unchanged by this feature; mix-prep tracks
are marked by artist-title selector (CLI) or via web feedback (`report_id` + `track_no`), both of
which work for the new section. No feedback-scope expansion.

Snapshot tests updated deliberately in the same commit as the renderer change.

## Reasons (`src/pipeline/reasons.py`)

- New fact: `download_count`. The `soundcloud_popularity` signal gets 2–3 deterministic template
  variants (stable-hash selection, per house style), e.g.
  "Free DL — grabbed 214 times on SoundCloud."
- Precedence: slotted at the `bandcamp_discovery` tier (source-signal level), below
  known_artist/label/chart/cross-source.

## Artifact, audition page, web

- Section-label map changes are covered in "Report rendering" above (all four paths).
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
  lane floor + count caps, popularity signals — SoundCloud signal fires only at/above threshold and
  only for soundcloud, **and the Mixupload signal regression-proven never to fire for SoundCloud**
  — mix-prep block equivalents (including the harmonic matches-before-demoted ordering inside the
  block, and demoted lane items still placing), empty-lane behavior.
- **Reasons:** matrix rows for the new signal's variants.
- **Report:** weekly + mix-prep snapshots updated deliberately; numbering continuity across the
  new section.
- **Artifact/web API:** section present with label; `test_web_api` report-detail passes unchanged
  (free-string key).
- **Config:** new keys' defaults and plumbing.

## README (same pass, per repo rules)

- Configuration docs: the four new `pipeline` keys and two `scoring` keys, with defaults.
- Scoring signals table: new SoundCloud popularity row; Mixupload row's wording corrected to
  reflect the source gate.
- Report structure description ("How it works" step 5 / sections list): Free Downloads section in
  both report kinds, exclusive-lane semantics in one sentence.

## Rollout

Feature branch off `develop` → PR → merge → release (next minor, v0.15.0) via the standard
deploy-release runbook. Backend-only: no tunefinder-web release required. Validation before PR:
full pytest, `check-config`, and a `run --dry-run` + `mix-prep <genre> --dry-run` pasted for review
showing the section populated.
