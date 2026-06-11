# Phase 1 Implementation Spec — Deterministic Reports, Genre Cleanup, Drift Removal

**Date:** 11 June 2026 · **Parent plan:** `docs/improvement-plan.md` (§2–§4) · **Audience:** the implementing Claude Code session.

This spec removes design decisions from implementation. Execute the commits **in order**; each commit leaves the suite green. Where this spec and the code disagree, stop and ask — do not improvise. Do not refactor, rename, or reformat anything not named here. Match existing style: plain functions, dataclasses, `from src...` imports, module-level `logger`.

**Supersession note:** this work intentionally overrides `CLAUDE.md`'s "keep LLM provider logic in `src/llm.py`" / "preserve LLM fallback behavior" guidance AND `AGENTS.md`'s entire "LLM And Reporting" section ("Keep the two-stage report pipeline intact", "preserve deterministic fallbacks when LLM calls fail"). Removing the LLM layer is the goal — do not stop-and-ask over this specific conflict. Both files are updated in Commit 8.

**Out of scope (do not touch):** scoring weights and section caps in `ranker.py`; all fetchers except the Juno deletion and the `catalog.py` guard; `dedup.py` normalisation regexes; `output/discord.py`; no manual edits to anything under `data/`, `fixtures/`, or `.env` (runtime writes by the pipeline — including Commit 7's `data/archive/` — are expected and fine).

---

## Commit 1 — `chore: remove dead config, models, and dependencies`

1. `config/settings.yaml`: delete the `max_candidates: 100` line from `pipeline:`; delete the entire `discogs:` block under `sources:` (no fetcher exists for it).
2. `src/config.py`: delete the `pipeline_max_candidates` property.
3. `src/models.py`: delete the `LabelRelevance` dataclass; delete `Candidate.is_known` and `Candidate.is_previously_recommended` fields; delete `ArtistProfile.associated_labels` field.
4. `src/pipeline/profile.py`: remove `associated_labels` from `_profile_to_dict` and `_dict_to_profile`. Loading older `artist_profiles.json` files that still contain the key must keep working (`_dict_to_profile` builds explicitly, so the stray key is simply ignored — verify with a test).
5. `requirements.txt`: remove `trafilatura` and `schedule` (verified unused — no imports anywhere).
6. `src/fetchers/catalog.py`: guard against tracklist numbering leaking into artist names (real data contains `"15. Zero T"`). Add module-level `_NUMBER_PREFIX_RE = re.compile(r"^\s*\d{1,3}[.)]\s+")` and a helper `_clean_artist(name: str) -> str` that strips it. Apply in `_parse_track` (to `artist`) and in `_parse_mix`'s `TrackRef` construction. Count strips per fetch and log once: `[catalog] Stripped numbering prefix from N artist names`.
7. `src/fetchers/catalog.py`: above `fetch_all_mixes`, add comment: `# Currently uncalled — retained deliberately for Phase 3 (BPM/energy-aware mix-prep). See docs/improvement-plan.md §5.`
8. Persisted dirty keys self-heal without intervention (verified): `cmd_run` and `cmd_mix_prep` both rebuild profiles + known_tracks from the catalog API on every run (step 1 in each) and overwrite `known_tracks.json`/`artist_profiles.json`. No data surgery. The root cause additionally lives upstream in `api.changsta.com`'s tracklist extraction — out of scope here, already noted in the plan (§4.10). The fixtures path (`_load_fixture_tracks`/`_load_fixture_mixes`) goes through the same parsers, so the guard is testable with plain dict fixtures.

**Tests:** new (or extended) `tests/test_catalog.py` — `_clean_artist` strips `"15. Zero T"` → `"Zero T"`, `"3) Foo"` → `"Foo"`, leaves `"808 State"`, `"2 Bad Mice"`, `"65daysofstatic"` untouched (prefix requires `.`/`)` + space); `_dict_to_profile` tolerates a dict containing legacy `associated_labels`.

---

## Commit 2 — `chore: delete defunct Juno source`

The site shut down June 2026; git history preserves the code.

1. Delete `src/fetchers/juno.py`.
2. `src/fetchers/__init__.py`: remove `juno` from the import line and the `("juno", juno.fetch)` entry from `_FETCHERS`.
3. `config/settings.yaml`: delete the entire `juno:` block. Also delete the now-stale comment lines above `release_date_window_days` that reference Juno chart window slugs — replace with: `# Release date window applied to all sources with known dates. Bandcamp items always pass (no date available).` Keep the value `28`.
4. `src/pipeline/dedup.py`: in `filter_known`'s docstring, replace the Juno-specific wording with: `Checks both the release-level title and any individual tracks nested in raw_metadata["tracks"], so a release whose tracks are already owned is correctly excluded.` Also fix the inline comment ~line 125 (`# Also check individual tracks embedded in the release (Juno EP tracks)` → drop the parenthetical). Keep the behaviour — other sources may populate `tracks` in future.
4b. Stale comment cleanup (verified — the only remaining Juno mentions outside the deleted files): `src/models.py` ~line 75, `SourceItem.source` comment `# e.g. "beatport", "juno", "bandcamp"` → `# e.g. "beatport", "volumo", "bandcamp"`; `src/pipeline/ranker.py` ~line 184, section comment `# --- Chart position (Juno and any other source that sets chart_position) ---` → `# --- Chart position (any source that sets chart_position, e.g. Beatport) ---`. (The Juno example inside `report.py`'s `_enrich_reasons` is deleted wholesale in Commit 6 — no action.)
5. `README.md`: remove Juno from the sources table, project structure listing, and any remaining prose mentions.

**Tests:** none exist for Juno (verified — no test or fixture references it); full suite must stay green.

---

## Commit 3 — `fix(config): remove Volumo loose-fit genre mappings`

Bass House is not UK bass. Volumo is ~72% of weekly corpus volume, so its loose fits dominate noise.

1. `config/settings.yaml`, `volumo.genres`: delete the three loose-fit entries — `uk-bass / id 2`, `funk-soul-jazz / id 17`, `hip-hop / id 29`. Keep `downtempo / id 18`. Replace the `# loose-fit mappings…` comment with: `# uk-bass, funk-soul-jazz, hip-hop intentionally not mapped — Volumo has no true-fit genre for them (see docs/improvement-plan.md §3).`
2. `README.md` genre-coverage table: Volumo column for `uk-bass`, `funk-soul-jazz`, `hip-hop` → `—`. Remove the `²` footnote and its markers (it covered exactly these mappings). Renumber the remaining footnote if needed.
3. `tests/test_volumo.py`: verified — no test asserts the production mappings, so expect zero test changes. **Caution:** the `genre_id` mismatch-filtering tests (compilation-guard block, ~line 346 on) build their own local settings fixtures that happen to use `{"name": "uk-bass", "id": 2}` as arbitrary test data — that is NOT the production mapping; leave those tests completely untouched.

**Do NOT** purge `data/candidate_pool.json` — stale tags age out naturally. **Ask Christophe** before changing the Beatport `rb → funk-soul-jazz` mapping; default is leave it.

---

## Commit 4 — `feat(ranker): expose per-label known-artist names`

Enabler for the deterministic label line. No behaviour change to scoring or sections.

1. `src/pipeline/ranker.py`, `_build_relevant_labels` (verified): currently returns `tuple[set[str], dict[str, int]]` — `(relevant, counts_int)`, where the internal `counts: dict[str, set[str]]` accumulates `profile.name.lower()` per label key (`c.label.lower().strip()`) and is collapsed via `len()` at line ~88. Add a parallel dict collecting `profile.name` (original case) per label key, and return a third value `label_artist_names: dict[str, list[str]]` = `{label_key: sorted(display_names)}` — **full list, no cap**: the renderer needs the true count for the `{n} of your artists` line; truncation to 3 happens at display time (Commits 5–6). Leave the existing two return values exactly as they are; update the docstring's `Return (relevant_labels, label_known_artist_counts)` line to match.
2. `rank_candidates` and `rank_candidates_mix_prep`: unpack the third value; **change both functions' return** to a 2-tuple `(sections, label_artist_names)`.
3. `tunefinder/__main__.py`: update both call sites to unpack — `sections, label_artists = rank_candidates(...)` (mix-prep likewise) — and do nothing else with `label_artists` in this commit (prefix `_` if lint complains). Report calls stay unchanged here; Commit 6 threads the value through. Each commit stays green on its own.
4. `tests/test_ranker.py`: update existing tests for the new return shape; add: a label with two known artists yields both display names sorted; a label with four yields all four (no cap); an unknown-artist label is absent from the map.

---

## Commit 5 — `feat(report): deterministic reason composer`

New module `src/pipeline/reasons.py`. Pure functions, no IO, no LLM.

### Public API

```python
def compose_reason(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    label_artists: dict[str, list[str]] | None = None,
    today: date | None = None,
) -> str:
```

`today` defaults to `datetime.now(timezone.utc).date()` in production (UTC, matching the current report code — not local `date.today()`). It MUST be an explicit parameter: `days_old` depends on it, so without injection the function is impure and every frozen snapshot rots within a day. All tests — and the Commit 6 snapshot fixtures — pass a fixed date.

### Variant selection — critical detail

Use **md5, not `hash()`** (builtin `hash()` is salted per process — snapshot tests would flake):

```python
import hashlib

def _variant(key: str, n: int) -> int:
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % n
```

Call with `key = c.key` so a given track always gets the same phrasing.

### Fact extraction (only ever state facts present in the data)

- `matched`: profiles matched via `_split_artists(c.artist)` (reuse `src.pipeline.profile._split_artists`), lowercased lookup in `profiles_lower`.
- `play_count`: max play_count among matched profiles. **Semantics warning (verified in `profile.py`):** `play_count` = Σ `track.recurrence_count` over the artist's distinct catalog tracks — total plays across the mix history, NOT the number of mixes. Never phrase it as "{n} of your mixes"; the old ranker explanation ("appears in N of your mixes") makes exactly this overclaim — do not copy it.
- `prior`: up to 2 titles from the best-matched profile's `track_titles` (best-matched = highest `play_count`; ties → first in `_split_artists` order), excluding case-insensitive match of `c.title`.
- `chart`: `c.raw_metadata.get("chart_position")` if `isinstance(int)` and `1 <= pos <= 100`.
- `sources`: `c.raw_metadata.get("seen_on_sources", [c.source])`.
- `days_old`: days between `c.release_date[:10]` and `today`, only if parseable and `0 <= days <= 60`.
- `label_names`: `label_artists.get(c.label.lower().strip(), [])` when `c.label` is set — verified: this is exactly the keying `_build_relevant_labels` uses (no helper exists, it's inline).
- `genre_disp`: first 2 of `c.genre_tags`, joined `"/"`; display `ukg` as `UKG`, otherwise as-is.
- `source_disp`: `c.source.title()`.

### Template table

Select the **first matching row** top-to-bottom (signal codes read from `c.signals`). `{a}` = first matched profile display name; `{n}` = play_count; `{p1},{p2}` = prior titles; `{pos}` = chart position; `{k}` = len(sources); `{srcs}` = comma-joined source display names; `{label}` = `c.label`; `{names}` = comma-joined **first 3** of label_names; `{g}` = genre_disp; `{d}` = days phrase ("today", "yesterday", "{d} days ago").

| Condition | Variants (choose via `_variant`) |
|---|---|
| `known_artist` AND chart | "You play {a} ({p1}) — now #{pos} on the {source_disp} {g} chart." · "{a} again — #{pos} on {source_disp} {g}; you've played {p1}." · "You play {a} — now #{pos} on the {source_disp} {g} chart." · "You play {a} — charting at #{pos} on {source_disp}." (the genre-free variant carries the row when `{g}` is empty; the prior-free variants carry it when prior is empty) |
| `known_artist` AND prior (no chart) | "New {a} — {n} plays across your mix history ({p1}, {p2})." · "{a} follow-up to {p1} — {n} plays in your mix history." |
| `known_artist` (no prior titles) | "{a} has {n} plays in your mix history — new material from them." |
| `label_match` AND label_names | "{label} — {names} release here; you play them all." (if 1 name: "{label} — home of {names}, who you play.") · "On {label}, the label behind {names} in your crates." |
| `label_match` (no names available) | "{label} — a label connected to artists you play." |
| chart (no artist match) | "#{pos} on the {source_disp} {g} chart this week." · "Charting at #{pos} on {source_disp} {g}." · "#{pos} on the {source_disp} chart this week." (genre-free fallback variant) |
| `cross_source` (k ≥ 2) | "Picked up by {k} stores this week ({srcs})." · "Surfaced on {k} sources: {srcs}." |
| `bandcamp_discovery` | "Independent Bandcamp find — {g}, outside the chart feeds." |
| `genre_match` AND days_old | "Fresh {g}{label_part}, out {d}." — where `{label_part}` = " on {label}" if label set, else "" |
| `genre_match` only | "Tagged {g} — inside your genre map." |
| fresh only | "Out {d}{label_part}." |
| fallback (no signals) | "New release{label_part} via {source_disp}." |

Rules: one sentence, ends with `.`, target ≤ 15 words; singular/plural handled ("{n} plays" with n=1 → "1 play"); when prior has only 1 title, drop `{p2}` and its comma.

**Eligibility (the mechanical meaning of "never emit empty placeholders"):** a variant is eligible only if every placeholder it references has a non-empty value. Filter to eligible variants first, then select via `_variant(c.key, len(eligible))`. A row with no eligible variants falls through to the next matching row; the fallback row is always eligible.

**Tests:** new `tests/test_reasons.py` — one test per table row (build candidates with exact signals/metadata, assert template facts appear); determinism (two calls → identical string); `hash`-independence (assert module uses md5 — e.g. same output across two `python -c` style invocations is impractical in pytest, so instead assert `_variant("x", 3)` equals a precomputed constant); banned-fact safety (candidate without chart_position never yields a "#" string; without label never mentions a label); eligibility fallback (known-artist-with-chart whose profile has no prior titles → the no-`{p1}` variant, never empty parens); variant coverage (same facts, different `c.key`s → exercise every variant of at least one multi-variant row); every test passes a fixed `today`.

---

## Commit 6 — `feat(report): deterministic renderer replaces LLM stages`

### Deletions

- `src/llm.py`, `src/pipeline/label_cache.py`, `tests/test_llm.py` — delete files (verified: `label_cache` is imported only by `report.py`; no `tests/test_label_cache.py` exists). Leave `data/label_profiles.json` on disk untouched.
- `tests/test_smoke.py` (verified: line 6 imports `src.llm.call_stage1` — the suite goes red without this): replace that import and its `assert callable(call_stage1)` with `from src.pipeline.reasons import compose_reason` + `assert callable(compose_reason)`.
- `src/pipeline/report.py`: delete `_enrich_reasons`, `_enrich_label_synopses`, `_clean_llm_json`, `_format_section_for_prompt`, `_format_label_watch_for_prompt`, `_DJ_CONTEXT`, `_signal_summary` (already dead — zero callers), both LLM system/user prompt builders, `call_stage1`/`call_stage2` imports, and `_fallback_report` / `_fallback_mix_prep_report` (their logic is absorbed below).

### Keep (verbatim behaviour)

`_sanitize_report`, `_build_footer`, `_format_fetcher_health`, `_build_mix_prep_header`, `_format_weekly_stats`, `_format_mix_prep_stats`. Discord chunking lives in `output/discord.py` and is untouched.

### New rendering (in `report.py`)

```python
def generate_report(sections, report_id, stats, settings,
                    profiles=None, label_artists=None, today=None) -> str
def generate_mix_prep_report(sections, report_id, stats, genre, settings,
                             profiles=None, label_artists=None, today=None) -> str
```

`profiles` stays a `dict[str, ArtistProfile]` exactly as today. Both functions build `profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}` once per report (same construction the ranker and `_format_weekly_stats` already use) and pass it, `label_artists`, and `today` into every `compose_reason` call. `today` (a `date`, default `datetime.now(timezone.utc).date()` — preserving the current code's UTC date semantics) also drives the `{today}` header — production omits it; tests and snapshots always inject a fixed date.

Weekly layout (mix-prep analogous, with its existing header and `## 🔺 Top Picks ({genre})` / `## 🎧 Deep Cuts` sections):

```
**TuneFinder — {today} ({report_id})**
*{weekly stats line}*

## 🔺 Top Picks
1. **Artist — Title** [Label] [Source] → [Listen](<url>)
> {reason}
2. ...

## 🏷️ Label Watch
**{Label display name}**
*{n} of your artists release here: {names}*
3. **Artist — Title** [Source] → [Listen](<url>)
> {reason}

## 👁️ Artist Watch
...

## 🃏 Wildcards
...

{footer — unchanged _build_footer output}
```

Rules: track numbering is **continuous across the whole report** (one counter, never resets — it is the reference key for the future `mark` command); omit a section header entirely when it has no tracks; omit `[Label]` bracket when label is None; omit the `[Source]` bracket never (always present); omit `→ [Listen]` when link is empty; Label Watch groups by label exactly as the old `_format_label_watch_for_prompt` grouped (label header bold, then italic artist-fact line — `*{n} of your artists release here: {names}*` where `{n}` = the full `len(label_artists[key])` and `{names}` = first 3 sorted names with `, …` appended when truncated (never claim "3 of your artists" when 5 do — the count is a stated fact); singular: `*1 of your artists releases here: {name}*` — omit the line entirely when no names are known), no-label tracks last without a sub-header; reasons come from `reasons.compose_reason`; everything passes through `_sanitize_report` before return; date format stays `%-d %B %Y`.

### Wiring & config

- `tunefinder/__main__.py`: both report calls already pass `profiles=profiles` (verified, `cmd_run` and `cmd_mix_prep`) — keep that argument and add `label_artists=label_artists` from Commit 4's unpacking. Dropping `profiles` at this call site is invisible to unit tests (they construct their own calls) — only the renderer test below and the DoD dry-run check catch it. `cmd_check_config` — delete the cascade printing and the function-local `PROVIDER_ENV_VAR` import; print the validated env summary plus a line `Report generation: deterministic (no LLM)`.
- `requirements.txt`: no change (verified: `src/llm.py` imports only `re`, `time`, `requests` — and `requests` is shared with the fetchers).
- `src/config.py`: `_REQUIRED_ENV_VARS` → `["DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID"]`; delete `PROVIDER_ENV_VAR`, the four `*_api_key` properties, and all four `llm_*` properties.
- `config/settings.yaml`: delete the entire `llm:` block.
- `.env.example`: remove the `# LLM` section (keep Discord and Volumo entries).

### Tests

- Rewrite `tests/test_report.py`: keep the four `_format_*_stats` tests as-is; replace all LLM-mocking tests with renderer tests: section omission when empty, continuous numbering across sections, label grouping + italic artist line, a label with 4 known artists renders the true count with first 3 names + `, …`, no-label fallthrough, `[Label]` omission, link omission, sanitiser still applied (a bare URL in a reason gets stripped), and the full profile plumbing: a `generate_report` call whose `profiles` dict matches a candidate's artist must produce an artist-grounded reason ("You play…" / "{n} plays…") in the output — this is the only test that guards the renderer→composer profile path.
- New snapshot tests: build a fixed 5-candidate fixture (2 top picks incl. one known-artist-with-chart, 1 label-watch with 2 known label artists, 1 artist-watch, 1 wildcard with no label/link) plus a 2-candidate mix-prep fixture. Fixtures pin every time-dependent input: pass `today=date(2026, 6, 11)` and fixed `release_date`s — verified sufficient: `_build_footer` renders only `report_id` + `stats`, no wall-clock values, so injecting `today` fully pins both snapshots. **Process:** generate the output once, print it, include both outputs in your completion message for Christophe's review, then freeze them as expected constants in the test. Do not hand-write the expected strings.

---

## Commit 7 — `feat(sources): archive weekly source snapshots`

Enabler for future offline replay/backtesting (improvement-plan §5).

1. `src/fetchers/__init__.py`: add `archive_source_items(items, data_dir, report_id)` — writes `{data_dir}/archive/source_items_{report_id}.json.gz` (gzip + same `_item_to_dict` shape), creates the dir, then prunes oldest files beyond 26 **by file mtime**. (Verified: `make_report_id()` is zero-padded `f"{year}-W{week:02d}"`, so filename sort would also work today — mtime simply doesn't couple retention to the id format.) Same-week re-runs overwrite the same file, which is the desired idempotency. Log path + retained count.
2. `tunefinder/__main__.py` `cmd_run`: call it right after `save_source_items(...)`, passing `report_id`. Not in dry-run? **Archive in both modes** — it is read-only history, harmless and useful.
3. If `data/` contents are git-ignored, the archive inherits that; do not add new git tracking for it.

**Tests:** `tests/test_sources_archive.py` with `tmp_path` — file written, gzip round-trips to the same items, prune keeps newest 26 (give files distinct mtimes via `os.utime`).

---

## Commit 8 — `docs: v0.7.0 changelog, README, CLAUDE.md, spike erratum`

1. `CHANGELOG.md`: new `## v0.7.0` entry covering: deterministic report generation (LLM stages removed — rationale one-liner + pointer to `docs/improvement-plan.md` §2), required env vars reduced to Discord only, Volumo loose-fit mappings removed, Juno deleted, dead config/models/deps removed, artist numbering-prefix guard, source snapshot archiving, track numbering in reports. (Heads-up: `README.md` also has its own small `## Changelog` section near the top — keep the two consistent if it lists recent versions.)
2. `README.md` consistency pass: "How it works" step 5 → deterministic rendering; delete the "LLM cascade" section; env vars section reduced; commands section unchanged except `check-config` description; sources table minus Juno; genre table per Commit 3; **fix the Volumo row/notes — audio previews DO work** (tested 11 Jun 2026).
3. `CLAUDE.md`: replace the LLM-related guidance ("Keep LLM provider logic in `src/llm.py`", the "preserve … LLM fallback behavior" clause, and the intro's "generates a report via LLM cascade") with: "Report rendering is deterministic — reasons in `src/pipeline/reasons.py`, layout in `src/pipeline/report.py`. Keep rendering free of network/LLM dependencies. Snapshot tests guard exact output; update them deliberately, never casually." Also remove `src/llm.py` from the module list and change the tests line's "mock external IO (LLM HTTP calls, Discord)" to "mock external IO (Discord)". Keep everything else.
3b. `AGENTS.md`: same pass — fix the Purpose bullet ("generates an LLM-written report" → deterministic report), remove the Architecture bullet "`src/llm.py` owns the provider cascade logic", and replace the entire "## LLM And Reporting" section with a short "## Reporting" section: deterministic rendering, reasons in `reasons.py`, Discord-safe formatting/link sanitisation preserved in `report.py`, snapshot tests guard exact output. Keep every other section as-is.
4. `docs/spikes/2026-06-04-volumo-source-spike.md`: add one line directly under the title: `> **Erratum (2026-06-11):** audio previews on Volumo DO work, contrary to §8 below — confirmed by manual testing.` Do not edit the body.
5. `com.openclaw.tune-finder.plist` and launchd docs: unchanged.
6. Version: verified — no version string exists outside `CHANGELOG.md` (no pyproject, no `__version__`, no README badge). The new `## v0.7.0` entry is the only version change; do not edit historical CHANGELOG entries.

---

## Definition of done (run all, paste outputs)

```
./venv/bin/pytest tests/ -v
./venv/bin/python -m tunefinder check-config
./venv/bin/python -m tunefinder run --dry-run
./venv/bin/python -m tunefinder mix-prep house --dry-run
grep -rni "llm\|mistral\|groq\|gemini\|deepseek\|openrouter" src/ tunefinder/ config/ requirements.txt .env.example
# expect: zero provider names and zero imports; the only acceptable "llm" hits are
# strings stating the LLM is gone (check-config's output line, a docstring saying "no LLM")
```

Suite green; the grep shows only the acceptable hits above; check-config passes with only Discord env vars required; both dry-run reports render with continuously numbered tracks, reasons on every track — **including artist-grounded ones ("You play…", "{n} plays…"); their total absence means `profiles` never reached the renderer** — label artist lines in Label Watch, and the standard footer; no LLM call attempts appear anywhere in the logs. Paste the two dry-run reports and both snapshot strings for review. Stop there — no deploy.
