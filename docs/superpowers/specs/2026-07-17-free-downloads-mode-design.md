# Free Downloads Mode — Design

**Date:** 2026-07-17
**Status:** Awaiting review
**Prerequisite:** Free-downloads section/lane (shipped v0.15.0 — see
`2026-07-17-free-downloads-section-design.md`)
**Repos affected:** TuneFinder (backend + CLI), tunefinder-web (SPA)

## Context

The free-downloads lane (`pipeline.free_download_sources: [soundcloud]`) routes
SoundCloud free-DL/bootleg tracks into an exclusive "Free Downloads" section in
the weekly and mix-prep reports. It works well enough that a dedicated run mode
is wanted: *"here's a genre, show me all the free stuff available for it"* —
leaning hard on the SoundCloud API without polluting the two existing modes.

The mode is named after the **lane**, not SoundCloud: any future free source
added to `pipeline.free_download_sources` (e.g. Bandcamp name-your-price)
joins the mode with zero further changes.

## Decisions (agreed 2026-07-17)

| Question | Decision |
|---|---|
| History | **Shared with mix-prep history.** A track shown in a free-downloads run never re-appears in a mix-prep's Free Downloads section, and vice versa. `mark`/`stats` work unchanged. |
| Report size | **30 tracks** default, config-driven. |
| Discord channel | **Reuse the mix-prep channel.** No new channel/config. |
| Genre argument | **Required**, same choices as mix-prep (`MIX_PREP_GENRES`). All-genres sweep is backlog. |
| Architecture | **Flag on the mix-prep engine** (`MixPrepOptions.free_only`), not a duplicated run function and not a shared-core refactor of the behaviour-verbatim `runs.py`. |
| SoundCloud API extras | All four: gated free DLs, server-side BPM filter, attribution accuracy, repost-aware popularity (each refined below). |
| Web UX | **Mode toggle on the existing workbench page** (`/mix-prep`), no new nav tab. |

## 1. CLI

```
tunefinder free-downloads <genre> [--bpm MIN-MAX] [--key KEY] [--no-bpm-flex] [--dry-run]
```

- `genre` choices = `MIX_PREP_GENRES` (single source of truth, as mix-prep).
- `--bpm` / `--key` / `--no-bpm-flex` / `--dry-run` reuse the existing mix-prep
  arg parsing and validation verbatim.
- Report: single section titled **"Free Downloads — {genre}"**, 30 slots,
  posted to the mix-prep Discord channel.
- Report id: `{make_report_id()}-free-dl-{genre}` (e.g. `2026-W29-free-dl-dnb`)
  — distinguishes these runs in history, `stats`, and the web UI without new
  plumbing.

## 2. Engine (`src/services/runs.py`)

`MixPrepOptions` gains `free_only: bool = False`. `run_mix_prep` branches:

1. **Fetch restriction** — `fetch_all_sources` gains an optional
   `only_sources: list[str] | None` parameter (a filter on the `_FETCHERS`
   loop, same "fetchers may ignore" precedent as `target_genre`). When
   `free_only`, pass `settings.pipeline_free_download_sources`.
2. **Pool injection restriction** — the candidate pool holds Beatport/Bandcamp
   leftovers; when `free_only`, injected pool candidates are filtered to lane
   sources too. Without this, paid store tracks leak into a free report.
3. **Lane slot count** — `pipeline.free_downloads_mode_count` (default 30)
   instead of `mix_prep_free_downloads_count`. Passed to
   `rank_candidates_mix_prep` as an optional lane-count override parameter.
4. **Report** — title "Free Downloads — {genre}"; empty store sections
   (Top Picks / Deep Cuts, which receive nothing since every candidate is a
   lane source) are omitted, not rendered as empty headers.
5. **Identity** — report id suffix `-free-dl-{genre}`; artifact kind
   `"free-downloads"`.

Unchanged and deliberately inherited: run lock, profile refresh with degraded
fallback, known-track filter, shared **mix-prep history** read/append, genre
filter + genre exclusions, release-date window, BPM/key harmonic
partition (demote-don't-drop), skip-penalty, label-affinity update, artifact +
audition page, dry-run gating, Discord delivery to the mix-prep channel.

## 3. SoundCloud fetcher (`src/fetchers/soundcloud.py`)

Grounded in the official OpenAPI spec (developers.soundcloud.com, fetched
2026-07-17). All work with the existing app-token auth.

### 3.1 Gated free downloads (biggest gem-unlock)

Most bootlegs are not native SoundCloud downloads — they sit behind
Hypeddit/ToneDen-style "Free DL" gates, where `downloadable` is `false` but
`purchase_title` says otherwise. These are currently excluded entirely by
`downloadable_only: true`.

- New config `sources.soundcloud.include_gated_free: true`.
- Heuristic: keep a non-downloadable track when `purchase_title` contains
  `free` (case-insensitive — covers "FREE DOWNLOAD", "Free DL") **or** the
  `purchase_url` host is a known gate domain: `hypeddit.com`, `toneden.io`,
  `gate.fm`.
- Kept tracks get `raw_metadata["free_gate"] = True`; native downloads keep
  `downloadable: true`. Report rendering distinguishes them (native ⬇️ vs
  gate 🔗) in Discord text, the audition page, and the web track cards.
- Scope note: the weekly and mix-prep Free Downloads sections gain gated
  tracks too — same fetcher, same lane meaning. Intentional.

### 3.2 BPM & key extraction

- Extract track `bpm` and `key_signature` into `raw_metadata["bpm"]` /
  `raw_metadata["key"]` on every fetch. The existing `--bpm`/`--key` harmonic
  machinery (`partition_by_harmonic`, `to_camelot`) consumes these
  immediately — demote-don't-drop, so untagged tracks are never lost.
- **Free-downloads runs only:** when a BPM range is provided, also pass
  `bpm[from]`/`bpm[to]` server-side on `GET /tracks` — strict semantics
  (only BPM-tagged tracks return), as agreed. Weekly and mix-prep runs never
  send the server param, so their lane behaviour is unchanged: extraction +
  demote-don't-drop only.
- **Must be live-verified during implementation** — this API documents
  `created_at[from]` filtering yet silently ignores it (verified 2026-07-17).
  If the server ignores `bpm[]` too, free-downloads `--bpm` degrades to the
  same client-side demote-don't-drop path; the server param is an
  optimisation, not a dependency.
- Plumbing: `fetch_all_sources` and fetcher signatures gain an optional
  `bpm_range` kwarg (ignored by fetchers that don't support it — same pattern
  as `target_genre`); `run_mix_prep` forwards `options.bpm_range` to the fetch
  only when `free_only`.

### 3.3 Attribution accuracy

Prefer `metadata_artist` (the real artist, when it differs from the uploader's
username) over `user.username` when non-empty. Accepted edge: a rare
previously-seen track may re-key once under its corrected artist name and
resurface; permanent attribution improvement outweighs a one-time repeat.

### 3.4 Release dates — deliberately narrowed

`release_year/month/day` are **not** adopted as the pipeline release date: a
bootleg of a 2005 tune uploaded yesterday carries `release_year: 2005` and the
28-day window would kill it — exactly the gems this mode hunts. Upload date
(`created_at`) stays canonical ("when it became available" is the correct
freshness signal for bootlegs). The release fields are stored in
`raw_metadata` for future display only.

## 4. Scoring (`src/pipeline/ranker.py`, `reasons.py`)

Repost-aware popularity: the SoundCloud popularity signal fires when
`download_count >= soundcloud_popularity_downloads` (existing, 50) **or**
`reposts_count >= soundcloud_popularity_reposts` (new key, default 25).
`reposts_count` is captured in `raw_metadata`. The deterministic reason line
extends to name reposts when that is the trigger. All other signals, weights,
and filters unchanged.

## 5. Config & docs

New keys, all with code defaults so existing configs work untouched:

```yaml
pipeline:
  free_downloads_mode_count: 30        # slots for the free-downloads mode report

sources:
  soundcloud:
    include_gated_free: true           # keep Hypeddit/ToneDen-style "Free DL" gated tracks

scoring:
  soundcloud_popularity_reposts: 25    # reposts_count that also earns the popularity signal
```

Plus `src/config.py` accessors, `settings.yaml` comments, and a README section
for the new command.

## 6. Web API (TuneFinder backend)

- `src/web/schemas.py`: every `kind`/`mode` literal gains `"free-downloads"` —
  report kinds (`ReportSummary`, `ReportDetail`) and run modes (`RunRequest`
  plus the job-status models that echo the mode back). The feedback `history`
  literal **stays** `weekly | mix-prep` — free-downloads records live in the
  mix-prep history.
- `src/web/app.py` reports-list `kind` query pattern →
  `^(weekly|mix-prep|free-downloads)$`.
- `src/web/reportdata.py` `report_kind()`: new branch — `-free-dl-` in the
  report id → `("free-downloads", genre)`.
- Run-job dispatch: `mode == "free-downloads"` →
  `run_mix_prep(settings, MixPrepOptions(..., free_only=True))`.
- The artifact track payload carries `free_gate` so clients can badge gated vs
  native downloads.

## 7. tunefinder-web (SPA — separate repo, second PR)

- **Workbench mode toggle**: segmented control on `/mix-prep` switches
  *Mix prep ↔ Free downloads*. Identical form (genre grid, BPM range + flex,
  key wheel, dry run); page copy and submit button reflect the mode; submit
  sends `mode: "free-downloads"`. No new nav tab (mobile bar is at six items).
- **Reports page**: third kind filter tab ("Free DLs") and a distinct
  kind-dot colour.
- **`src/lib/reportName.ts`**: parse `2026-W29-free-dl-dnb` →
  `Free downloads · dnb`. *Adjacent bug, fix in same pass:* the existing
  `MIXPREP_RE` expects `mix-prep-YYYY-MM-DD-genre` but the backend emits
  `YYYY-Www-mix-prep-genre`, so mix-prep reports render as raw ids today —
  verify against live data and correct the pattern.
- **Feedback mapping (critical):** the report/bag feedback path sends
  `history: detail.kind`. Kind `"free-downloads"` must map to history
  `"mix-prep"`, or one-tap marks from a free-downloads report fail backend
  validation.
- **Track cards**: gate badge (🔗 gate link vs ⬇️ free download) from
  `free_gate`.
- `src/reports/artifact.ts` local kind union widened;
  `npm run generate-types` regen from the updated backend.

## 8. Testing & rollout

**TuneFinder:** gate-heuristic unit tests (purchase_title variants, gate
domains, negatives like "Buy"/empty); bpm/key/`metadata_artist` extraction;
search-URL `bpm[]` construction; `free_only` restricting fetch **and** pool
injection; shared-history append + report-id suffix; lane-count override;
deliberate new report snapshot for the mode; `report_kind` derivation; schema
literal/pattern tests. Validation: `check-config`, full pytest, live
`--dry-run` (verifies server-side `bpm[]` behaviour).

**tunefinder-web:** vitest for `reportName` (new + fixed patterns), kind tab
filtering, kind→history feedback mapping, mode-toggle submit payload.

**Rollout order:** TuneFinder PR first — the API change is additive, and a
stale frontend renders new-kind rows harmlessly (worst case: mix-prep dot
colour, raw id title). Then tunefinder-web: type regen, UI, deploy via the
rsync runbook.

## Backlog (explicitly out of scope)

- `/tracks/{urn}/related` — "more like this gem" seeded from liked marks.
- `/playlists?q=free download <genre>` — curated free-DL playlist mining.
- `/users/{urn}/tracks` + reposts — bootleg-uploader / tastemaker following.
- All-genres sweep (`free-downloads` without a genre).
- Surfacing original-release year as a display hint on bootlegs.
