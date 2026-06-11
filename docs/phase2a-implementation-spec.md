# Phase 2a Implementation Spec — Feedback Capture & Ops Hardening

**Date:** 11 June 2026 · **Parent doc:** `docs/improvement-plan.md` §5 (Phase 2) · **Baseline:** v0.7.1 (scoring hygiene live) · **Audience:** the implementing Claude Code session.

Phase 2a is the small slice of Phase 2 that unblocks everything else: persist *what* was recommended and *why* (signals), capture outcomes (`mark`), read them back (`stats`), and harden the fetch layer (retry + anomaly alerts). Every deferred scoring decision waits on this data, and data accrues one report per week — so this ships before the next weekly run.

**Deferred to 2b (do not build):** audition-queue HTML, `tunefinder explain`, Rekordbox library diff, daily `healthcheck` command (Commit 6's health file is its substrate; the command comes later).

**Resolved decisions (Christophe, 11 Jun 2026):**
- `mark` by **track number** resolves against the latest **weekly** report only. The **"Artist - Title"** selector form searches weekly history first, then mix-prep history (a purchase is a purchase, whichever report surfaced it).
- Outcome vocabulary: `bought` | `liked` | `skip` | `own`. `own` means "I already had this" — it is an identity-gap signal (the known-track filter should have caught it) and `stats` reports it separately, not as a hit or miss.
- Feedback is append-only; `stats` uses the latest entry per **(history, key)** — weekly and mix-prep marks for the same track coexist.

**Out of scope (do not touch):** all scoring logic in `ranker.py` (weights, signals, section assignment) — the one sanctioned `ranker.py` change is Commit 2's deletion of the non-scoring helper `all_section_candidates`; `reasons.py`; the *rendered report text* — internal refactors of `report.py` are allowed in Commit 1, but the snapshot strings `_WEEKLY_SNAPSHOT` and `_MIX_PREP_SNAPSHOT` in `tests/test_report.py` must remain byte-identical and untouched; dedup keys and `build_history_keys` semantics (regressions cause duplicate recommendations); pool logic — with one sanctioned exception: Commit 2 changes which list the `recommended_keys` set in `cmd_run` derives from (`report_order` instead of `all_section_candidates`); same element set, pool behaviour identical, and nothing else pool-related may move; fetcher *parsing* (Commit 5 touches transport only); anything under `data/`, `fixtures/`, `.env`. **No new runtime dependencies** — stdlib only (`requirements-dev.txt` additions are fine per CLAUDE.md).

Work on `develop`. The checkout may be sitting on `main` — switch before any change: `git fetch origin && git checkout develop && git pull --ff-only origin develop && git merge --ff-only origin/main`. If the `--ff-only` merge refuses, the branches have diverged since this spec was written — stop and ask. Execute commits in order; each leaves the suite green.

**No Step 0 baseline this phase:** nothing here changes scoring or rendering, so there is no before/after diff to review. The invariant is stronger: the frozen snapshots prove render parity, and `run --dry-run` still writes `data/source_items.json` + an archive snapshot (gitignored — leave alone, never commit or clean).

---

## Commit 1 — `feat(report): canonical report ordering helper`

Track numbers are currently assigned only at render time (`track_counter` closure in `generate_report` / `generate_mix_prep_report`), and **Label Watch regroups tracks by label during rendering** (`by_label` dict, first-occurrence label order, then `no_label`). So the rendered numbering diverges from raw `sections["label_watch"]` order whenever labels interleave. `mark <n>` needs one authoritative ordering.

1. `src/pipeline/report.py`: extract the Label Watch grouping into a module-level helper:
   ```python
   def _group_label_watch(label_watch: list[Candidate]) -> tuple[dict[str, list[Candidate]], list[Candidate]]:
       """(by_label in first-occurrence order, no_label) — the exact render grouping."""
   ```
   Refactor the Label Watch block inside `generate_report` to consume it. Rendered output must not change.
2. Add the canonical ordering function, same module:
   ```python
   _SECTION_ORDER = ("top_picks", "label_watch", "artist_watch", "wildcards", "deep_cuts")

   def report_order(sections: dict[str, list[Candidate]]) -> list[Candidate]:
       """Candidates in exact rendered-number order. Walks _SECTION_ORDER (absent
       keys skipped); 'label_watch' is expanded via _group_label_watch (grouped
       labels first, then no-label tracks). Raises ValueError on a section key
       not in _SECTION_ORDER. Works for weekly and mix-prep section dicts."""
   ```
   **Ordering contract:** `report_order` hardcodes the same fixed sequence the renderers hardcode (weekly renders top_picks, label_watch, artist_watch, wildcards; mix-prep renders top_picks, deep_cuts) — it does **not** rely on dict insertion order, so a caller-built dict in any key order yields identical numbering. The `ValueError` on unknown keys makes a future new section fail loudly instead of silently vanishing from history records. The order test below is the guard that `report_order` and the renderers agree.

**Tests** (`tests/test_report.py` — *add* tests, do not touch the snapshot constants): build the existing snapshot fixture sections, render, parse the leading `N.` numbers + artist/title from the output, and assert they equal `enumerate(report_order(sections), start=1)`. Cover an interleaved-label Label Watch fixture (e.g. [labelA, labelB, labelA]) where raw order ≠ rendered order; a sections dict built in shuffled key order producing identical `report_order` output; and the `ValueError` on an unknown section key. Both snapshot tests still pass unchanged — that is the proof the refactor is behaviour-neutral.

---

## Commit 2 — `feat(history): persist track numbers, signals, genres, score`

`RecommendationRecord` (`src/models.py`) currently stores artist/title/link/source/recommended_at/report_id — nothing `stats` can attribute outcomes to.

1. `src/models.py`, `RecommendationRecord`: add optional fields, all defaulted so old data loads:
   ```python
   track_no: Optional[int] = None          # rendered number in its report
   signal_codes: list[str] = field(default_factory=list)  # e.g. ["known_artist", "genre_match"]
   genre_tags: list[str] = field(default_factory=list)
   score: Optional[float] = None
   label: Optional[str] = None
   ```
   The `key` property and everything `build_history_keys` reads are untouched.
2. `src/pipeline/history.py`: extend `_record_to_dict` / `_dict_to_record` symmetrically; `_dict_to_record` uses `.get` with the same defaults (old files must round-trip).
3. `tunefinder/__main__.py`, both `cmd_run` and `cmd_mix_prep`: replace `all_section_candidates(sections)` with `report_order(sections)` (import from `src.pipeline.report`) and build records via `enumerate(..., start=1)`:
   `track_no=i`, `signal_codes=[s.code for s in c.signals]`, `genre_tags=c.genre_tags`, `score=c.score`, `label=c.label`.
   In `cmd_run`, `recommended_keys` for pool exclusion derives from the same list — same element set, only order changed, so pool behaviour is identical.
4. `src/pipeline/ranker.py`: delete `all_section_candidates` — verified zero callers remain after step 3 (its two call sites are both `__main__.py`; no test imports it).
5. Runtime note, not an implementer action: the first live run after deploy rewrites the history JSON files with null/empty-defaulted new fields on legacy records — `append_records` has always rewritten the whole file. Expected; no migration script, no manual edits to `data/`.

**Tests** (`tests/test_history.py`): round-trip a record with all new fields; load a legacy dict (missing all new fields) and assert defaults + unchanged `key`; assert `build_history_keys` output is identical for legacy and extended records — this is the duplicate-recommendation regression guard.

---

## Commit 3 — `feat(cli): tunefinder mark`

1. New `src/pipeline/feedback.py`:
   - `FeedbackEntry` dataclass: `key, artist, title, outcome, marked_at, report_id, track_no (Optional[int]), history ("weekly"|"mix-prep")`. `key` is the **normalised** `make_dedup_key(record.artist, record.title)` of the resolved record — the same function resolution matches on — so entries stay joinable across raw title variants. `artist`/`title` store the record's raw display values.
   - `OUTCOMES = ("bought", "liked", "skip", "own")`.
   - `load_feedback(data_dir) / append_feedback(entry, data_dir)` → `data/feedback.json`, append-only list, same json style as `history.py`.
   - `resolve_selector(selector, weekly_records, mix_prep_records)` → `tuple[RecommendationRecord, str]` (record, history-name), raising `LookupError` with an explanatory message when resolution fails:
     - Selector is treated as a number when `selector.isdigit()`, else as a string.
     - Integer selector: latest weekly report = records sharing the `report_id` of the max-`recommended_at` weekly record; match `track_no`. **Tie-break:** a same-week live rerun appends records under the same `report_id`, so a `track_no` can legitimately repeat within it — take the match with the latest `recommended_at` (the report currently on screen). If the latest report has no `track_no` values (pre-v0.8.0 records), raise with a message saying numbers exist only for reports generated after v0.8.0 — use the string form.
     - String selector: split once on `" - "` → (artist, title); compare via `make_dedup_key(artist, title)` (import from `src.pipeline.dedup`) against each record's `make_dedup_key(r.artist, r.title)`. Because matching is *by* dedup-key equality, every match is the same logical track (raw title variants, or the same track in both histories) — there is no ambiguous case. Search weekly newest-first, then mix-prep newest-first; take the first (newest) match. Zero matches → `LookupError` with a "no recommended track matches" message.
2. `tunefinder/__main__.py`: `cmd_mark(args)` + subparser: positionals `selector` and `outcome` (choices=`OUTCOMES`). Calls `load_settings()` for `data_dir` but **not** `settings.validate()` — `mark` must work without Discord env vars. Re-marking the same **(history, key)** appends a new entry (audit trail); print a note when a previous outcome existed for that (history, key) — an existing mark on the same track in the *other* history is not a re-mark and prints no note. Print confirmation: `Marked #14 Artist — Title as bought (2026-W24)`; omit the `#n` part when the resolved record has no `track_no`.

**Tests** (new `tests/test_feedback.py`, tmp-dir `data_dir`): number resolution against latest report only (older report same number not matched); same-`report_id` rerun tie-break; pre-v0.8.0 error path; string resolution incl. raw-variant match (record "Title (Original Mix)" found by "Artist - Title"), mix-prep fallback, and zero-match error; append + re-mark behaviour; round-trip.

---

## Commit 4 — `feat(cli): tunefinder stats`

1. `src/pipeline/feedback.py`: pure aggregation, no printing:
   ```python
   def summarise_feedback(weekly, mix_prep, entries) -> dict
   ```
   Join rule: entries ↔ records via `make_dedup_key(r.artist, r.title)` — the same function `mark` resolves with — and an outcome attributes only to records in the entry's own `history` (a track marked from the weekly report does not also credit its mix-prep twin). Latest entry per **(history, key)** wins — not per key globally: weekly filtering consults only weekly history and mix-prep only mix-prep history, so the same track can legitimately be recommended and marked in both, and a later mix-prep mark must not erase the weekly mark from weekly stats. Buckets: overall **segmented weekly vs mix-prep** — recommended / marked / coverage % computed per history (mix-prep reports run ~40 tracks; pooling them with weekly would crater the coverage metric); positive rate = bought+liked over marked, `own` excluded from both numerator and denominator; by `signal_code` (a marked record contributes its outcome to every code it carries; records with empty `signal_codes` bucket as `"(pre-v0.8.0)"`); by `source`; by `genre_tag` (empty `genre_tags` buckets as `"(pre-v0.8.0)"` likewise); by `report_id` (chronological). `own` reported as its own line: count + the note that these are known-track-filter misses (identity gaps).
2. `tunefinder/__main__.py`: `cmd_stats(args)` + bare subparser. No `validate()`. Plain `print`, deterministic ordering (sort by descending marked-count, then name). No feedback file → friendly "no feedback recorded yet — mark tracks with `tunefinder mark`" and exit 0.

**Tests** (`tests/test_feedback.py`): fixture of records + entries covering every bucket, latest-entry-wins **per (history, key)** — including the regression case where a weekly mark and a later mix-prep mark share the same key and both remain represented in their own history's stats — pre-v0.8.0 bucketing, `own` exclusion from hit rate, empty-feedback path. Assert on the returned dict, not stdout.

---

## Commit 5 — `feat(fetchers): bounded retry with jitter in common.py`

`get_html` / `post_html` are single-attempt; one transient timeout kills a source for the week. Bandcamp's silent 3-week death is the cautionary tale — but Cloudflare 403s must NOT be hammered.

1. `src/fetchers/common.py`: module constants `_RETRY_ATTEMPTS = 3`, `_RETRY_BACKOFFS = (2.0, 5.0)` (sleep before attempt 2 and 3, each `+ random.uniform(0, 0.5)` jitter). Internal helper `_request_with_retry(method, url, **kwargs)` used by both `get_html` and `post_html` (public signatures unchanged — fetchers untouched):
   - Retry on: `requests.Timeout`, `requests.ConnectionError`, and `HTTPError` with status ≥ 500 or == 429.
   - Never retry other 4xx (403/404 raise immediately — bot blocks and dead pages don't heal in 5 seconds).
   - Exhausted attempts re-raise the last exception (fetch_all_sources already catches per-fetcher and records health).
   - Log each retry at warning level with attempt number and cause.
2. `parse_rss` inherits retry via `get_html` — no change.
3. Retrying POSTs is deliberate and safe here: fetcher POSTs are read-only queries (search/filter forms), not mutations. Note this in a comment next to the helper.

**Tests** (new `tests/test_common_retry.py`, `unittest.mock.patch` on `requests.get`/`requests.post` — no new dev deps): succeeds-after-one-timeout; 500-then-200; 403 raises immediately with exactly one call; three-timeouts exhausts and raises; jitter/sleep patched out via `time.sleep` mock.

---

## Commit 6 — `feat(pipeline): per-source anomaly alerts from run health history`

Bandcamp died silently for ~3 weeks. `fetch_all_sources` already returns per-source health (`{source: {count, error}}`) and an alert channel + `post_alert` already exist (`src/output/discord.py`, prefix `⚠️ ALERT |`). What's missing is run-over-run memory.

1. New `src/pipeline/source_health.py`:
   - `append_run_health(health, data_dir, report_id)` → `data/source_health.json`: append `{report_id, run_at (ISO), health}`, retain the most recent 26 entries (matches archive retention).
   - `load_run_health(data_dir) -> list[dict]`.
   - `detect_anomalies(current_health, prior_runs, drop_threshold_pct, min_history_runs) -> list[str]`, pure:
     - any source with `error` set → `"{source}: FAILED — {error}"` (no history needed);
     - `count == 0`, no error → `"{source}: 0 items (was averaging {avg})"` when history exists, plain `"0 items"` otherwise;
     - `count` < `drop_threshold_pct`% of the source's trailing-4-run mean (non-error runs only) → drop message with count vs average. Drop detection only activates with ≥ `min_history_runs` prior data points for that source (cold-start guard).
2. `config/settings.yaml`: new top-level block + `src/config.py` properties `alerts_source_drop_threshold_pct` (int, default 50) and `alerts_min_history_runs` (int, default 2):
   ```yaml
   alerts:
     # Alert when a source's count falls below this % of its trailing-4-run average.
     source_drop_threshold_pct: 50
     # Prior runs required per source before drop detection activates.
     min_history_runs: 2
   ```
3. `tunefinder/__main__.py`, `cmd_run` only (mix-prep counts are genre-narrowed — not comparable run-to-run; do not record or alert there): after `fetch_all_sources`, load prior runs **before** appending the current one, compute anomalies; if not `dry_run`: `append_run_health(...)` and, when anomalies exist, one `post_alert` with all anomaly lines joined. In `dry_run`: log anomalies at warning level, write nothing, post nothing.
4. **Fix the pre-existing dry-run alert leak** (both commands): the no-candidates branches call `post_alert` unguarded (`cmd_run` ~line 152, `cmd_mix_prep` ~line 322), so a dry-run with zero candidates posts a live Discord alert today. Guard both with `if not dry_run`, logging the alert text at warning level in dry-run instead. No unit test (CLI wiring isn't unit-tested — same precedent as Phase 1.5 Commit 5); this is what makes the DoD's "never post live" true on every path.

**Tests** (new `tests/test_source_health.py`): error always alerts; drop below threshold alerts; above threshold doesn't; cold start (fewer than `min_history_runs`) suppresses drop detection but not error alerts; retention prunes to 26; trailing mean ignores error runs.

---

## Commit 7 — `docs: v0.8.0 changelog and README`

1. `CHANGELOG.md`: `## v0.8.0` — minor bump is correct: two new CLI commands and an extended history record schema (backward-compatible). One line per Commit 1–6 with rationale, pointer to `docs/improvement-plan.md` §5 Phase 2.
2. `README.md`: commands table (`mark`, `stats` with selector/outcome syntax); new `alerts:` config keys; data-files section gains `feedback.json` and `source_health.json`; operational note: **track-number marking works from the first weekly report generated after this deploys** — earlier reports have no stored numbers (use `"Artist - Title"`).
3. `AGENTS.md` needs **no** update: its "Useful commands" list is verification commands only, and `mark`/`stats` are not verification steps. Don't add them there.

---

## Definition of done (run all, paste outputs)

```
./venv/bin/pytest tests/ -v
./venv/bin/python -m tunefinder check-config
./venv/bin/python -m tunefinder run --dry-run
./venv/bin/python -m tunefinder mix-prep house --dry-run
```

Suite green. `git diff v0.7.1 -- tests/test_report.py` must show **no change to `_WEEKLY_SNAPSHOT` or `_MIX_PREP_SNAPSHOT`** — only added tests. `mark`/`stats` are proven by unit tests against a tmp data dir (live history untouched; never write to the real `data/` from tests). `mark` and `stats` must run without Discord env vars set. Paste both dry-run outputs as a sanity check (no scoring changed, so report shape should look like v0.7.1's). Never post live to Discord. Stop there — no deploy; Christophe triggers "deploy release" himself.
