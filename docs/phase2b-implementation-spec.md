# Phase 2b Implementation Spec — Audition Queue & Explain

**Date:** 12 June 2026 · **Parent doc:** `docs/improvement-plan.md` §5 (Phase 2) · **Baseline:** v0.8.0 (feedback capture live) · **Audience:** the implementing Claude Code session.

Two tools that compound the Phase 2a feedback loop. The **audition page** turns an 18-browser-tab listening session into one local HTML page with inline players — more auditioning means more `mark`s per week, and marks are the bottleneck resource. **`tunefinder explain`** traces any track through the pipeline offline — the tuning tool for the weight-fitting work that starts once marks accumulate.

**Deferred to 2c (do not build):** Rekordbox library diff (implicit "bought" capture), daily `healthcheck` command.

**Resolved decisions (Christophe, 12 Jun 2026):**
- Audition pages are written by BOTH `run` and `mix-prep`, **live runs only** (dry-run logs a skip note — consistent with the history-write policy), to `data/reports/audition_{report_id}.html`, retaining the most recent 26 pages (same policy as snapshot archives). The run logs the file path; **no auto-open** — launchd runs unattended.
- The page is fully self-contained: inline CSS, one tiny inline vanilla-JS clipboard function, **no CDN dependencies**. The only remote content is the store players themselves (iframes / audio elements), all `loading="lazy"`.
- Player precedence per track: Volumo direct-preview `<audio>` (if Step 0 finds a URL field) → Bandcamp `EmbeddedPlayer` iframe (album id) → Beatport embed iframe (track id) → link-only row. Every track always shows its store link and mark copy buttons regardless of player availability (number-form commands on weekly pages, string-form on mix-prep — see Commit 3).
- `explain` reconstructs from the **current** `data/` state only — no `--week` archive replay (that is the future `replay` command). Output is labelled a reconstruction: it can differ from the live run if sources or the profile changed since.

**Out of scope (do not touch):** `reasons.py` (the audition page calls `compose_reason` as-is — reasons match Discord by construction); `report.py` entirely — `report_order`, `_SECTION_ORDER`, and `_group_label_watch` are *imported* by the new module, nothing in `report.py` changes, so `_WEEKLY_SNAPSHOT` / `_MIX_PREP_SNAPSHOT` stay byte-identical trivially; all scoring weights and signal logic — `explain` *imports* ranker internals, and the one sanctioned `ranker.py` change is Commit 5's inert trace hook in `_assign_sections` (default-off, behaviour-identical, parity-tested); dedup **grouping/key semantics** — Commit 1's metadata backfill inside `_merge_group` is the one sanctioned dedup change and it must not alter which items group or which item wins; fetcher fetch/parse logic beyond the exact metadata-capture lines in Commit 2; `mark`/`stats`/history/pool/feedback semantics; anything under `data/`, `fixtures/`, `.env` — with the usual runtime-behaviour clarification: live runs creating and pruning `data/reports/` (Commit 4) is the product working as designed, not the implementer editing local state; never hand-edit, seed, or commit anything under `data/`. **No new runtime dependencies, no LLM, no external JS/CSS.**

Work on `develop`. The checkout may be sitting on `main` — switch before any change: `git fetch origin && git checkout develop && git pull --ff-only origin develop && git merge --ff-only origin/main`. If the `--ff-only` merge refuses, stop and ask. Execute commits in order; each leaves the suite green.

---

## Step 0 — live embed probes (not a commit; ~15 minutes; record outcomes in the DoD summary)

Three facts the spec cannot pin from code alone. None block the build — every negative outcome falls back to link-only rows. Run these on the Mac with real network:

1. **Volumo preview field.** The fetcher discards unknown track fields, so hit the albums API once (reuse the fetcher's own URL: build with `_build_url` in a Python REPL, or copy a logged URL from a recent run) and inspect one `track` object. Looking for a direct preview/sample/stream URL field (names like `preview`, `sample_url`, `prelisten`, `audio`). **Found:** record the exact field name for Commits 1/2/3. **Not found:** Volumo rows are link-only; delete the Volumo branch from Commit 3's precedence, the volumo capture from Commit 2, and the preview placeholder from Commit 1's `_MERGE_BACKFILL_KEYS`.
2. **Bandcamp album id + embed.** POST the `discover_web` payload once (copy `_fetch_tag`'s payload) and confirm result items carry a numeric album id — expected field `id`. Then load `https://bandcamp.com/EmbeddedPlayer/album={that_id}/size=small/tracklist=false/artwork=small/` in a browser and confirm it renders a playable widget for an arbitrary new release. **Id field missing:** Bandcamp rows are link-only — also drop the bandcamp capture from Commit 2 and `bandcamp_album_id` from Commit 1's allowlist. **Id present but embed broken:** keep the capture (cheap, future-proof); rows are link-only.
3. **Beatport embed pattern.** Take one `beatport_id` from `data/source_items.json` and load `https://embed.beatport.com/?id={id}&type=track` in a browser. Confirm or correct the working embed URL shape. Not working → Beatport rows are link-only.

---

## Commit 1 — `fix(dedup): preserve embed metadata across cross-source merges`

`_merge_group` keeps the richest item's `raw_metadata` wholesale, so a track seen on Beatport+Volumo loses the losing source's ids/preview — and cross-source tracks are precisely the high-scoring ones the audition page most needs players for.

1. `src/pipeline/dedup.py`: module constant
   ```python
   # Embed/display metadata worth preserving from merged-away duplicates.
   # Backfill only — the winning item's values are never overwritten.
   _MERGE_BACKFILL_KEYS = ("beatport_id", "volumo_track_id", "volumo_album_id",
                           "bandcamp_album_id", "<volumo preview field per Step 0>",
                           "bpm", "key", "keysign")
   ```
   In `_merge_group`, after `best` is chosen: for each key in `_MERGE_BACKFILL_KEYS`, if `best.raw_metadata.get(key)` is `None`/absent, take the first non-empty value from the other items in the group. Grouping, winner selection (`_richness`), genre-tag union, and `seen_on_sources` are untouched.

**Tests** (`tests/` — extend the existing dedup coverage wherever it lives; create `tests/test_dedup.py` if it doesn't): winner's existing values never overwritten; missing keys backfilled from losers; non-allowlisted loser keys NOT copied; no backfill keys are added for single-item groups (note: `_merge_group` already sets `seen_on_sources` even for one-item groups — that existing behaviour stays exactly as is, don't assert its absence).

---

## Commit 2 — `feat(fetchers): capture embed metadata`

Narrow additions to the `raw_metadata` dicts only — no fetch or parsing logic changes.

1. `src/fetchers/bandcamp.py`: add the numeric album id to `raw_metadata` — expected `"bandcamp_album_id": item.get("id")` with the field name confirmed in Step 0 (comment the observed source field).
2. `src/fetchers/volumo.py`: per Step 0 outcome, capture the preview URL field in `_parse_track`'s `raw_metadata` (skip this file entirely if Step 0 found none).

**Tests:** extend existing fetcher fixture tests to assert the new keys. **`tests/test_volumo.py` warning:** its genre-id tests use `uk-bass`/`id 2` as local fixture data — that is deliberate fixture content, do not "fix" it.

---

## Commit 3 — `feat(pipeline): audition page renderer`

1. New `src/pipeline/audition.py`, pure and deterministic — **no network, no filesystem** in the render function:
   ```python
   def generate_audition_page(sections, report_id, settings,
                              profiles=None, label_artists=None,
                              mark_by_number: bool = True,
                              today: Optional[date] = None) -> str
   ```
   - **Structure:** `report_order` returns a flat list, which cannot carry section headers. The renderer walks the structure itself — iterate `_SECTION_ORDER` (import from `src.pipeline.report`), skip absent keys, expand `label_watch` via `_group_label_watch`, and advance ONE page-wide counter per track. Tests assert the flattened (number, candidate) sequence equals `enumerate(report_order(sections), start=1)` — numbering identical to the Discord report and therefore to `mark`.
   - Per track render: number, artist — title, label, source(s) (`seen_on_sources` fallback `[c.source]`), genre tags, BPM and key when present (`raw_metadata` keys `bpm`, then `keysign` or `key`), the reason line via `compose_reason(c, profiles_lower, label_artists=label_artists, today=today)` — build `profiles_lower` exactly as `generate_report` does (`{k.lower(): v ...}`); `today` injected for test determinism, the player per the precedence in Resolved decisions, the store link, and four copy buttons (`bought` / `liked` / `skip` / `own`). **The copied command depends on the report type:** `mark` by number resolves against the latest *weekly* report only (Phase 2a resolved decision), so weekly pages copy `tunefinder mark {n} {outcome}` while mix-prep pages must copy the string form `tunefinder mark {shlex.quote(f"{artist} - {title}")} {outcome}`. Add a `mark_by_number: bool = True` parameter to `generate_audition_page`; `cmd_mix_prep` passes `False`. Use `shlex.quote` (stdlib) for the string form so quotes in titles can't break the copied shell command, and put the full command in a `data-cmd` attribute (HTML-escaped) read by `copyCmd(this)` — do **not** inline it into the `onclick` JS string literal, where shell, JS, and HTML quoting layers collide.
   - Section headers come from that walk (section key, prettified). Label Watch does NOT need per-label sub-headers — the `_group_label_watch` expansion already yields grouped order, and per-track label is displayed anyway.
   - Player markup: Volumo `<audio controls preload="none" src="{preview_url}">`; Bandcamp `<iframe loading="lazy" src="https://bandcamp.com/EmbeddedPlayer/album={bandcamp_album_id}/size=small/tracklist=false/artwork=small/">` (URL per Step 0); Beatport `<iframe loading="lazy" src="...">` per Step 0. Escape all interpolated text (`html.escape`); ids/URLs from `raw_metadata` are interpolated only when present and of the expected type (`int` for ids, `str` startswith `http` for preview URLs).
   - Single inline `<style>` block and a single inline `<script>` defining exactly one function, matching the `data-cmd` mechanism: buttons carry `onclick="copyCmd(this)"` and `function copyCmd(button) { navigator.clipboard.writeText(button.dataset.cmd); }`. Header shows report_id + date + track count; footer shows "generated by TuneFinder v-deterministic — reasons identical to the Discord report".

**Tests** (new `tests/test_audition.py`): snapshot test with a frozen fixture covering every player branch **that survives Step 0** plus the always-present link-only row — branches deleted by a failed probe don't exist, so don't write aspirational tests for them (generate → review → freeze, the Phase 1 process; inject `today`). Expose the fixture builder as a module-level `build_fixture_sections()` returning exactly `(sections, report_id, settings, profiles, label_artists)` — `settings` may be `None`: the renderer accepts it for signature symmetry with `generate_report` and must not read it — so the DoD one-liner is executable as written; numbering matches `report_order` enumeration; weekly fixture emits number-form mark commands and a `mark_by_number=False` fixture emits `shlex.quote`d string-form commands (cover a title containing a quote); escaping test with a hostile title (`<script>` in a track name must come out escaped, including inside `data-cmd`); missing-metadata tracks render link-only.

---

## Commit 4 — `feat(pipeline): write audition pages from runs`

1. `src/pipeline/audition.py`: add
   ```python
   def write_audition_page(html: str, data_dir: str, report_id: str) -> str  # returns path
   ```
   Writes `{data_dir}/reports/audition_{report_id}.html` (`os.makedirs(..., exist_ok=True)`), then prunes `audition_*.html` in that directory beyond the most recent 26 by mtime — same pattern as `archive_source_items`.
2. `tunefinder/__main__.py`, `cmd_run` and `cmd_mix_prep`: after the report is generated, if not `dry_run`: build the audition HTML (`generate_audition_page(...)` with the same `sections`/`profiles`/`label_artists` already in scope; `cmd_mix_prep` passes `mark_by_number=False`), `write_audition_page(...)`, and log the returned path at info level. In `dry_run`: skip generation entirely and log "DRY RUN — audition page not written" — an unrendered string has no observable effect, so don't build one tests can't assert on.

**Tests** (`tests/test_audition.py`): write + retention pruning against a tmp dir (seed 27 fake pages, assert oldest pruned); path format for weekly and mix-prep report_ids.

---

## Commit 5 — `feat(cli): tunefinder explain`

1. New `src/pipeline/explain.py`:
   ```python
   def explain_track(selector: str, settings) -> str   # deterministic multi-line text
   ```
   Selector: `"Artist - Title"`, split once on `" - "` and matched by `make_dedup_key` — the exact convention `mark` uses. `explain` traces the **weekly** pipeline (mix-prep filtering differs and is out of scope; say so in the header line). **Target-candidate resolution rule:** the deduped fetched candidate when present; else the pool candidate (`pool_to_candidates` of its `PoolRecord`); else the selector is unknown — report the verdicts that remain meaningful from the key alone (known-track, history, feedback) and skip scoring/section reconstruction, which need candidate metadata. Loads, offline: `load_source_items`, `load_known_tracks`, `load_artist_profiles` (all exist), weekly history, pool (`load_pool`), feedback (`load_feedback`). Trace, in pipeline order:
   - **Fetched:** every raw `SourceItem` matching the key — source, link, label, release_date, genre_tags per copy. None found → say "not in the current week's fetch" and continue — a pool-resident selector still enters scoring and section reconstruction via the pool-injection step below.
   - **Dedup:** re-run `deduplicate_source_items` over the full loaded set; report the merged item's genre union and `seen_on_sources`.
   - **Known-track filter:** when a target candidate exists, compute the verdict by running it through `filter_known([c], load_known_tracks(...))` itself (unknown selectors use the key-only membership check per the resolution rule) — do **not** reimplement the key checks; the real filter also checks nested `raw_metadata["tracks"]` titles, and a reimplementation would miss release-level exclusions.
   - **History filter:** verdict via `filter_history([c], build_history_keys(weekly history))`; if previously recommended, say which `report_id`/date (look the record up directly).
   - **Release window:** verdict via `filter_release_date([c], settings.pipeline_release_date_window_days)` (covers the no-date pass-through for free); skip when the window is unset.
   - **Score reconstruction — single pass only:** `_score` mutates in place and *appends* signals on every call, so the same `Candidate` object must never be scored twice. There is exactly ONE full-set scoring pass (the one section reconstruction below needs): rebuild the context as `cmd_run` does — `items_to_candidates` over the deduped set as `label_seed`, `_build_genre_set` + `_build_relevant_labels` + `recent_recommended_artists` — then filter, pool-inject, and `_score` every candidate once. Read the target's signal codes, explanations, and total off its candidate from that pass — this covers fresh *and* pool-only selectors alike, since pool injection puts them in the pass (with their `pool_age` penalty). Only when the target is absent from the pass (filtered earlier, or pool-excluded by known/history keys) score a **fresh `Candidate` copy** against the same context, labelled `(hypothetical — track was excluded before scoring)`. Import these from `src.pipeline.ranker` (importing the underscore-prefixed functions is sanctioned for this diagnostic module — do not copy their logic).
   - **Section reconstruction:** the downstream half of the single pass above — filter the whole candidate set (known, history, release window), **inject pool candidates** exactly as `cmd_run` does (`pool_to_candidates`, excluding fresh-key duplicates, known and history keys — without this the reconstructed sections misstate the real run), score everything once, sort, then run `_assign_sections`; report which section the track landed in, or why not. **Skip-reason support requires a hook:** `_assign_sections` returns only the selected sections, so add an optional `trace: dict | None = None` parameter. `pick()` runs once per section, so the trace must be **per-section**: give `pick()` a `section_name` argument (used only for tracing) and, when `trace` is provided, append `(section_name, reason)` to `trace.setdefault(id(c), [])` for each examined-and-skipped candidate. Reasons: `below floor {x}`, `lacks {require_signal} signal`, `artist cap`, `release cap`, `genre cap ({g})`. Default `None` changes nothing. A candidate with no entry for a section and not selected was never examined there — report it as `outscored — {section} filled before its rank (position {p} of {n})`. **Ranker tests for the hook (in `tests/test_ranker.py`):** sections identical with and without `trace`; trace contents correct for one fixture per skip reason, including the per-section multiplicity (same candidate skipped in two sections for two different reasons). (`_assign_sections_mix_prep` gets no hook — `explain` is weekly-only.)
   - **Pool:** if the key is in the pool, report `added_at` and `last_score`.
   - **Feedback:** if marked, report outcome + when.
   - Header line: `Reconstruction from current data/ state (source_items.json of the last fetch) — not a replay of the posted report.`
2. `tunefinder/__main__.py`: `cmd_explain(args)` + subparser, positional `selector`. `load_settings()` but no `validate()` — works without Discord env vars, like `mark`/`stats`.

**Tests** (new `tests/test_explain.py`, tmp data_dir with synthetic JSON files): one test per verdict — known-filtered, history-filtered, window-dropped, scored-and-sectioned (assert signal lines appear), floor-blocked, pool-resident, marked-with-feedback, completely unknown selector. Assert on returned string content, not stdout.

---

## Commit 6 — `docs: v0.9.0 changelog and README`

1. `CHANGELOG.md`: `## v0.9.0` — minor bump (new `explain` command, audition page output, embed metadata capture + dedup backfill). One line per Commit 1–5, pointer to `docs/improvement-plan.md` §5 Phase 2.
2. `README.md`: audition pages (where they're written, retention, which stores play inline vs link out — per Step 0 outcomes); `explain` usage with an example trace; note the dedup metadata backfill under the dedup section if one exists.
3. `AGENTS.md`: no update — same rationale as Phase 2a (`explain` is operator tooling, not a verification command).

---

## Definition of done (run all, paste outputs)

```
./venv/bin/pytest tests/ -v
./venv/bin/python -m tunefinder check-config
./venv/bin/python -m tunefinder run --dry-run
./venv/bin/python -m tunefinder mix-prep house --dry-run
./venv/bin/python -m tunefinder explain "<artist - title from data/source_items.json>"
```

Suite green. `git diff v0.8.0 -- tests/test_report.py` must be **empty** (nothing in this phase touches report rendering or its tests). Both dry-runs must log the audition skip note and write **no** files under `data/reports/`. Paste the Step 0 probe outcomes (which players are live vs link-only). Render the fixture page to a stable path and open it:
```
./venv/bin/python -c "from tests.test_audition import build_fixture_sections; from src.pipeline.audition import generate_audition_page; import pathlib; from datetime import date; pathlib.Path('/tmp/audition-fixture.html').write_text(generate_audition_page(*build_fixture_sections(), today=date(2026, 6, 1)))"
```
— executable as-is given `build_fixture_sections()`'s pinned 5-tuple return; confirm layout renders and at least one surviving player type plays (network permitting). Paste one `explain` trace output. Never post live to Discord. Stop there — no deploy; Christophe triggers "deploy release" himself.
