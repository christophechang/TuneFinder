# Free Downloads Report Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Free-download/bootleg tracks (SoundCloud today) get their own exclusive report section in weekly and mix-prep reports instead of competing — and losing — against store releases.

**Architecture:** Config-driven lane routing (`pipeline.free_download_sources`) partitions lane candidates out of section assignment before the store sections run; a dedicated section with its own floor/counts is ranked by combined score plus a new source-gated SoundCloud popularity signal. Rendering is added to all four output paths (weekly Discord, mix-prep Discord, report artifact, audition page) with the artifact unified onto the shared `_SECTION_ORDER`.

**Tech Stack:** Python 3.11+, pytest, dataclasses. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-17-free-downloads-section-design.md` (approved; includes review-finding amendments).

## Global Constraints

- Work on branch `feat/free-downloads-section` off `develop`. Conventional commits, **no `Co-Authored-By` trailers**.
- TDD for every behavior: failing test → verify fail → minimal code → verify pass → commit.
- Run tests with `./venv/bin/pytest`; never a bare `pytest`.
- Do NOT touch: dedup identity, pool mechanics, history schema, fetchers, `.env`, `data/`, `fixtures/`.
- Snapshot tests (`tests/test_report.py`) are updated deliberately in the same commit as the renderer change, never casually.
- Signal code for both popularity signals is the existing `source_popularity` (explanation text carries the store name); the spec's name "soundcloud_popularity" refers to the config weights, not a new code.
- Never post live to Discord. Dry-run only for pipeline validation.

---

### Task 0: Branch

**Files:** none (git only)

- [ ] **Step 1: Create the branch**

```bash
cd /Users/christophechang/Development/TuneFinder
git checkout develop && git pull origin develop && git checkout -b feat/free-downloads-section
```

---

### Task 1: Config — lane pipeline keys

**Files:**
- Modify: `src/config.py` (after `pipeline_section_min_score`, ~line 127)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.pipeline_free_download_sources -> list[str]` (default `[]`),
  `Settings.pipeline_free_downloads_count -> int` (default `5`),
  `Settings.pipeline_mix_prep_free_downloads_count -> int` (default `10`),
  `Settings.pipeline_free_downloads_min_score -> float` (default `0.0`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
# ---------------------------------------------------------------------------
# Free-download lane pipeline keys
# ---------------------------------------------------------------------------


def test_free_download_lane_defaults():
    from src.config import Settings
    s = Settings({})
    assert s.pipeline_free_download_sources == []
    assert s.pipeline_free_downloads_count == 5
    assert s.pipeline_mix_prep_free_downloads_count == 10
    assert s.pipeline_free_downloads_min_score == 0.0


def test_free_download_lane_from_config():
    from src.config import Settings
    s = Settings({"pipeline": {
        "free_download_sources": ["soundcloud", "hypeddit"],
        "free_downloads_count": 3,
        "mix_prep_free_downloads_count": 7,
        "free_downloads_min_score": 0.5,
    }})
    assert s.pipeline_free_download_sources == ["soundcloud", "hypeddit"]
    assert s.pipeline_free_downloads_count == 3
    assert s.pipeline_mix_prep_free_downloads_count == 7
    assert s.pipeline_free_downloads_min_score == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/pytest tests/test_config.py -k free_download -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'pipeline_free_download_sources'`

- [ ] **Step 3: Implement the properties** (in `src/config.py`, directly after the `pipeline_section_min_score` property)

```python
    @property
    def pipeline_free_download_sources(self) -> list[str]:
        return self._data.get("pipeline", {}).get("free_download_sources", [])

    @property
    def pipeline_free_downloads_count(self) -> int:
        return self._data.get("pipeline", {}).get("free_downloads_count", 5)

    @property
    def pipeline_mix_prep_free_downloads_count(self) -> int:
        return self._data.get("pipeline", {}).get("mix_prep_free_downloads_count", 10)

    @property
    def pipeline_free_downloads_min_score(self) -> float:
        return float(self._data.get("pipeline", {}).get("free_downloads_min_score", 0.0))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/pytest tests/test_config.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: add free-download lane pipeline config keys"
```

---

### Task 2: ScoringWeights — SoundCloud popularity fields

**Files:**
- Modify: `src/pipeline/ranker.py:58-60` (weights dataclass, next to the Mixupload pair)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ScoringWeights.w_soundcloud_popularity: float = 0.25`,
  `ScoringWeights.soundcloud_popularity_downloads: int = 50` — loaded from `scoring:` YAML keys
  automatically (`scoring_weights()` introspects dataclass fields, `src/config.py:215-228`).

- [ ] **Step 1: Write the failing test** (append to `tests/test_config.py`)

```python
def test_scoring_weights_soundcloud_popularity_fields():
    from src.config import Settings
    s = Settings({"scoring": {"w_soundcloud_popularity": 0.5, "soundcloud_popularity_downloads": 10}})
    w = s.scoring_weights()
    assert w.w_soundcloud_popularity == 0.5
    assert w.soundcloud_popularity_downloads == 10
    defaults = Settings({}).scoring_weights()
    assert defaults.w_soundcloud_popularity == 0.25
    assert defaults.soundcloud_popularity_downloads == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/pytest tests/test_config.py::test_scoring_weights_soundcloud_popularity_fields -v`
Expected: FAIL — the config layer warns "Unknown scoring keys ignored" and the dataclass lacks the attribute (`AttributeError` on `w_soundcloud_popularity`).

- [ ] **Step 3: Add the fields** (in `src/pipeline/ranker.py`, directly after the Mixupload pair at lines 58-60)

```python
    # --- SoundCloud popularity signal (free-DL lane) ---
    w_soundcloud_popularity: float = 0.25      # bonus for SoundCloud tracks other DJs are downloading
    soundcloud_popularity_downloads: int = 50  # minimum download_count to fire the signal
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/pytest tests/test_config.py::test_scoring_weights_soundcloud_popularity_fields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/ranker.py tests/test_config.py
git commit -m "feat: add SoundCloud popularity scoring weight fields"
```

---

### Task 3: Ranker bug fix — source-gate the Mixupload popularity signal

Pre-existing bug: the condition at `src/pipeline/ranker.py:498-507` fires on `download_count`
alone; SoundCloud items (which carry `download_count` since v0.14.0) at ≥100 downloads currently
earn +0.25 and a false "downloads on Mixupload" signal.

**Files:**
- Modify: `src/pipeline/ranker.py:498-501`
- Test: `tests/test_ranker.py`

**Interfaces:**
- Consumes: existing `tests/test_ranker.py` helpers — `_candidate(artist=, title=, label=,
  source=, **kw)` (line 35) and `_score(c, profiles_lower, relevant_labels, label_counts,
  genre_set)` (imported at line 32 from `src.pipeline.ranker`).

- [ ] **Step 1: Write the failing regression test** (append to `tests/test_ranker.py` near the other `_score` tests)

```python
def test_mixupload_popularity_never_fires_for_soundcloud():
    """download_count is no longer Mixupload-only (SoundCloud carries it since
    v0.14.0) — the Mixupload signal must be source-gated."""
    c = _candidate(source="soundcloud", raw_metadata={"download_count": 500})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any("Mixupload" in s.explanation for s in c.signals)


def test_mixupload_popularity_still_fires_for_mixupload():
    c = _candidate(source="mixupload", raw_metadata={"download_count": 500})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert any(s.code == "source_popularity" and "Mixupload" in s.explanation for s in c.signals)
```

- [ ] **Step 2: Run to verify the first test fails**

Run: `./venv/bin/pytest tests/test_ranker.py -k mixupload_popularity -v`
Expected: `test_mixupload_popularity_never_fires_for_soundcloud` FAILS (signal present); the second PASSES.

- [ ] **Step 3: Gate the condition** (replace `src/pipeline/ranker.py:498-501`)

```python
    # --- Mixupload popularity signal (discovery axis; issue #12) ---
    # Source-gated: SoundCloud also carries download_count (v0.14.0) and must
    # never earn a "downloads on Mixupload" signal.
    download_count = c.raw_metadata.get("download_count")
    if (c.source == "mixupload" and download_count is not None and isinstance(download_count, int) and
        download_count >= weights.mixupload_popularity_downloads):
```

(The body of the block — score/discovery increments and the signal append — is unchanged.)

- [ ] **Step 4: Run the full ranker suite**

Run: `./venv/bin/pytest tests/test_ranker.py -v`
Expected: all PASS. If an existing test constructed a non-mixupload candidate relying on the
ungated signal, fix that test's `source` to `"mixupload"` — the old behaviour was the bug.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "fix: source-gate Mixupload popularity signal (SoundCloud carries download_count)"
```

---

### Task 4: Ranker — SoundCloud popularity signal

**Files:**
- Modify: `src/pipeline/ranker.py` (directly after the Mixupload popularity block)
- Test: `tests/test_ranker.py`

**Interfaces:**
- Produces: signal code `source_popularity` with explanation `"{n} downloads on SoundCloud."`,
  +`w_soundcloud_popularity` on `score` and `discovery` axes.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ranker.py`, same helpers as Task 3)

```python
def test_soundcloud_popularity_fires_at_threshold():
    c = _candidate(source="soundcloud", raw_metadata={"download_count": 50})
    base = _candidate(source="soundcloud", raw_metadata={})
    gs = _build_genre_set({})
    _score(c, {}, set(), {}, gs)
    _score(base, {}, set(), {}, gs)
    assert any(s.code == "source_popularity" and "SoundCloud" in s.explanation for s in c.signals)
    assert c.score == pytest.approx(base.score + 0.25)
    assert c.discovery_score == pytest.approx(base.discovery_score + 0.25)


def test_soundcloud_popularity_below_threshold_silent():
    c = _candidate(source="soundcloud", raw_metadata={"download_count": 49})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any(s.code == "source_popularity" for s in c.signals)


def test_soundcloud_popularity_not_for_other_sources():
    c = _candidate(source="bandcamp", raw_metadata={"download_count": 500})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert not any("SoundCloud" in s.explanation for s in c.signals)
```

- [ ] **Step 2: Run to verify they fail**

Run: `./venv/bin/pytest tests/test_ranker.py -k soundcloud_popularity -v`
Expected: first test FAILS (no signal); others pass vacuously or fail on score delta.

- [ ] **Step 3: Implement** (in `src/pipeline/ranker.py`, immediately after the Mixupload block)

```python
    # --- SoundCloud popularity signal (discovery axis; free-DL lane) ---
    if (c.source == "soundcloud" and download_count is not None and isinstance(download_count, int) and
        download_count >= weights.soundcloud_popularity_downloads):
        score += weights.w_soundcloud_popularity
        discovery += weights.w_soundcloud_popularity
        c.signals.append(RecommendationSignal(
            code="source_popularity",
            explanation=f"{download_count} downloads on SoundCloud.",
        ))
```

- [ ] **Step 4: Run to verify green**

Run: `./venv/bin/pytest tests/test_ranker.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat: SoundCloud download-count popularity signal"
```

---

### Task 5: Ranker — weekly lane partition + Free Downloads section

**Files:**
- Modify: `src/pipeline/ranker.py` `_assign_sections` (~lines 539-685)
- Test: `tests/test_ranker.py`

**Interfaces:**
- Consumes: Task 1 settings properties.
- Produces: `_assign_sections` return dict gains key `"free_downloads": list[Candidate]`;
  lane candidates (source ∈ `pipeline_free_download_sources`) appear ONLY there; `trace` gains
  `("sections", "routed to free_downloads lane")` and `("free_downloads", "below lane floor …")`
  entries.

- [ ] **Step 1: First extend the existing `_MockSettings` class** (tests/test_ranker.py, ~line 194)
  with lane defaults so every existing section test keeps passing unchanged:

```python
    pipeline_free_download_sources = []
    pipeline_free_downloads_count = 5
    pipeline_mix_prep_free_downloads_count = 10
    pipeline_free_downloads_min_score = 0.0
```

  then add a lane-enabled subclass next to it and the failing tests (using the file's existing
  `_candidate`/`_scored_candidate` helpers and direct `_assign_sections` calls):

```python
class _LaneSettings(_MockSettings):
    pipeline_free_download_sources = ["soundcloud"]


def test_free_download_lane_is_exclusive():
    """A lane candidate never enters store sections (even with a huge score);
    a store candidate never enters free_downloads."""
    sc = _scored_candidate(99.0, artist="Lane Artist", source="soundcloud")
    store = _scored_candidate(50.0, artist="Store Artist", source="beatport")
    sections = _assign_sections([sc, store], _LaneSettings(), _build_genre_set({}))
    all_store = [c for k in ("top_picks", "label_watch", "artist_watch", "wildcards")
                 for c in sections[k]]
    assert sc not in all_store
    assert sc in sections["free_downloads"]
    assert store not in sections["free_downloads"]


def test_free_download_lane_floor_and_cap():
    lane = [_scored_candidate(0.5, artist=f"DJ {i}", title=f"Boot {i}", source="soundcloud")
            for i in range(8)]
    sections = _assign_sections(lane, _LaneSettings(), _build_genre_set({}))
    assert len(sections["free_downloads"]) == 5  # cap 5; lane floor 0 admits 0.5-scorers


def test_free_download_lane_own_floor():
    class _FlooredLane(_LaneSettings):
        pipeline_free_downloads_min_score = 0.5

    c = _scored_candidate(0.4, source="soundcloud")
    sections = _assign_sections([c], _FlooredLane(), _build_genre_set({}))
    assert sections["free_downloads"] == []


def test_lane_disabled_when_no_sources_configured():
    sc = _scored_candidate(5.0, source="soundcloud")
    sections = _assign_sections([sc], _MockSettings(), _build_genre_set({}))
    assert sections["free_downloads"] == []
    # with no lane configured the candidate competes normally
    assert sc in sections["top_picks"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `./venv/bin/pytest tests/test_ranker.py -k free_download -v`
Expected: FAIL with `KeyError: 'free_downloads'`

- [ ] **Step 3: Implement the partition + lane pick** (in `_assign_sections`)

After `weights = settings.scoring_weights()` (line ~550), add:

```python
    # Free-download lane (exclusive): lane candidates never compete for store
    # sections, store candidates never take lane slots. See
    # docs/superpowers/specs/2026-07-17-free-downloads-section-design.md.
    lane_sources = set(settings.pipeline_free_download_sources)
    lane_n = settings.pipeline_free_downloads_count
    lane_floor = settings.pipeline_free_downloads_min_score
    free_dl_pool = [c for c in ranked if c.source in lane_sources]
    if lane_sources:
        ranked = [c for c in ranked if c.source not in lane_sources]
        if trace is not None:
            for c in free_dl_pool:
                trace.setdefault(id(c), []).append(("sections", "routed to free_downloads lane"))
```

After the wildcards assignment (line ~672), add:

```python
    def _pick_free_downloads() -> list[Candidate]:
        artist_counts: dict[str, int] = {}
        result: list[Candidate] = []
        for c in free_dl_pool:  # already score-descending
            if c.score < lane_floor:
                if trace is not None:
                    trace.setdefault(id(c), []).append(("free_downloads", f"below lane floor {lane_floor}"))
                continue
            artist_key = normalise_artist(c.artist)
            if artist_counts.get(artist_key, 0) >= MAX_PER_ARTIST:
                if trace is not None:
                    trace.setdefault(id(c), []).append(("free_downloads", "artist cap"))
                continue
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            result.append(c)
            if len(result) >= lane_n:
                break
        return result

    free_downloads = _pick_free_downloads()
```

Extend the summary log line and the return dict:

```python
    logger.info(
        f"[ranker] Sections — top_picks: {len(top_picks)}, "
        f"label_watch: {len(label_watch)}, artist_watch: {len(artist_watch)}, "
        f"wildcards: {len(wildcards)} (floor={min_score}), "
        f"free_downloads: {len(free_downloads)} (lane floor={lane_floor}) | genres: {genre_summary or 'none tagged'}"
    )
    return {
        "top_picks": top_picks,
        "label_watch": label_watch,
        "artist_watch": artist_watch,
        "wildcards": wildcards,
        "free_downloads": free_downloads,
    }
```

- [ ] **Step 4: Run the ranker suite**

Run: `./venv/bin/pytest tests/test_ranker.py -v`
Expected: all PASS (existing section tests unaffected — with `free_download_sources` unset the
partition is a no-op and the new key is an empty list; if any existing test asserts the exact
return-dict keys, extend its expectation with `"free_downloads": []`).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat: exclusive free-downloads lane in weekly section assignment"
```

---

### Task 6: Ranker — mix-prep Free Downloads block

**Files:**
- Modify: `src/pipeline/ranker.py` `_assign_sections_mix_prep` (lines 750-805)
- Test: `tests/test_ranker.py`

**Interfaces:**
- Consumes: Task 1 settings properties; existing `demoted_keys` semantics (line 753).
- Produces: return dict gains `"free_downloads": list[Candidate]`; lane block preserves
  matches-before-demoted ordering internally; demotion never excludes a lane item.

- [ ] **Step 1: Write the failing tests**

```python
def test_mix_prep_free_download_lane_exclusive_and_capped():
    lane = [_scored_candidate(0.5, artist=f"DJ {i}", title=f"B{i}", source="soundcloud")
            for i in range(12)]
    store = _scored_candidate(5.0, source="beatport")
    sections = _assign_sections_mix_prep([store] + lane, _LaneSettings())
    assert len(sections["free_downloads"]) == 10
    assert store not in sections["free_downloads"]
    assert all(c not in sections["top_picks"] and c not in sections["deep_cuts"] for c in lane)


def test_mix_prep_lane_preserves_harmonic_demotion_order():
    """Within the lane block, harmonic matches sort above demoted unknowns
    regardless of score — and demoted lane items still place."""
    match = _scored_candidate(0.5, artist="Match", title="M", source="soundcloud")
    demoted = _scored_candidate(3.0, artist="Unknown", title="U", source="soundcloud")
    sections = _assign_sections_mix_prep([demoted, match], _LaneSettings(),
                                         demoted_keys={demoted.key})
    assert sections["free_downloads"] == [match, demoted]
```

(Uses `_LaneSettings` from Task 5 — lane floor 0.0, mix-prep lane count 10. The second test passes
candidates score-descending — `[demoted, match]` — because the assignment contract receives
pre-ranked input.)

- [ ] **Step 2: Run to verify they fail**

Run: `./venv/bin/pytest tests/test_ranker.py -k mix_prep_free_download -v; ./venv/bin/pytest tests/test_ranker.py -k mix_prep_lane -v`
Expected: FAIL with `KeyError: 'free_downloads'`

- [ ] **Step 3: Implement** (in `_assign_sections_mix_prep`)

Replace the config-read head (lines 762-764) and partition after the demotion sort (lines 773-776):

```python
    top_n = settings.pipeline_mix_prep_top_picks_count
    deep_n = settings.pipeline_mix_prep_deep_cuts_count
    min_score = settings.pipeline_section_min_score
    lane_sources = set(settings.pipeline_free_download_sources)
    lane_n = settings.pipeline_mix_prep_free_downloads_count
    lane_floor = settings.pipeline_free_downloads_min_score
```

```python
    order = (
        sorted(ranked, key=lambda c: c.key in demoted_keys)
        if demoted_keys else ranked
    )
    # Free-download lane partition AFTER the demotion sort so the lane block
    # inherits matches-before-demoted ordering (spec: harmonic-filter interplay).
    free_dl_order = [c for c in order if c.source in lane_sources]
    if lane_sources:
        order = [c for c in order if c.source not in lane_sources]
```

Parameterise `pick` with pool and floor, and assign the third section:

```python
    def pick(n: int, pool: list[Candidate], floor: float) -> list[Candidate]:
        artist_counts: dict[str, int] = {}
        release_counts: dict[str, int] = {}
        result = []
        for c in pool:
            if id(c) in used:
                continue
            if c.score < floor:
                continue
            artist_key = normalise_artist(c.artist)
            release_key = (c.release_name or "").strip().lower()
            if artist_counts.get(artist_key, 0) >= MAX_PER_ARTIST:
                continue
            if release_key and release_counts.get(release_key, 0) >= MAX_PER_RELEASE:
                continue
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
            if release_key:
                release_counts[release_key] = release_counts.get(release_key, 0) + 1
            result.append(c)
            used.add(id(c))
            if len(result) >= n:
                break
        return result

    top_picks = pick(top_n, order, min_score)
    deep_cuts = pick(deep_n, order, min_score)
    free_downloads = pick(lane_n, free_dl_order, lane_floor)
    logger.info(
        f"[ranker] Mix-prep sections — top_picks: {len(top_picks)}, deep_cuts: {len(deep_cuts)} "
        f"(floor={min_score}), free_downloads: {len(free_downloads)} (lane floor={lane_floor})"
    )
    return {"top_picks": top_picks, "deep_cuts": deep_cuts, "free_downloads": free_downloads}
```

- [ ] **Step 4: Run the ranker suite**

Run: `./venv/bin/pytest tests/test_ranker.py -v`
Expected: all PASS (same extend-expectation note as Task 5 Step 4 for any exact-keys assertions).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat: free-downloads block in mix-prep section assignment"
```

---

### Task 7: Reasons — popularity reason line + SoundCloud display casing

**Files:**
- Modify: `src/pipeline/reasons.py` (fact extraction ~line 75, `source_disp` line 96,
  eligibility map ~line 127, `_fill` ~line 160, template table after the
  `bandcamp_discovery` branch ~line 235)
- Test: `tests/test_reasons.py`

**Interfaces:**
- Consumes: `source_popularity` signal (Tasks 3-4) and `raw_metadata["download_count"]`.
- Produces: deterministic reason line for popularity-signal tracks; `{source_disp}` renders
  "SoundCloud" (brand casing) everywhere.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_reasons.py`, mirroring its
  existing candidate/profile fixture style)

```python
def test_source_popularity_reason_soundcloud():
    c = _candidate(source="soundcloud",
                   signals=[RecommendationSignal("source_popularity", "214 downloads on SoundCloud.")],
                   raw_metadata={"download_count": 214})
    reason = compose_reason(c, {})
    assert "214" in reason
    assert "SoundCloud" in reason           # brand casing, not "Soundcloud"
    assert "Soundcloud" not in reason


def test_source_popularity_reason_deterministic():
    c = _candidate(source="soundcloud",
                   signals=[RecommendationSignal("source_popularity", "80 downloads on SoundCloud.")],
                   raw_metadata={"download_count": 80})
    assert compose_reason(c, {}) == compose_reason(c, {})
```

- [ ] **Step 2: Run to verify they fail**

Run: `./venv/bin/pytest tests/test_reasons.py -k source_popularity -v`
Expected: FAIL — no popularity branch exists, so the reason falls through to a genre/fresh
fallback with no "214"; casing assertion also fails if the fallback happens to include the source.

- [ ] **Step 3: Implement the four edits**

(a) Fact extraction, after the `chart` block (~line 75):

```python
    dl_count: Optional[int] = None
    raw_dl = c.raw_metadata.get("download_count")
    if isinstance(raw_dl, int) and raw_dl > 0:
        dl_count = raw_dl
```

(b) Brand-cased source display (replace line 96):

```python
    source_disp = {"soundcloud": "SoundCloud"}.get(c.source, c.source.title())
```

(c) Eligibility map — add alongside the existing entries (~line 127):

```python
            "{dl}": dl_count is not None,
```

(d) `_fill` — add alongside the existing replacements (~line 160):

```python
        result = result.replace("{dl}", str(dl_count) if dl_count is not None else "")
```

(e) Template branch — insert directly AFTER the `bandcamp_discovery` branch (~line 235):

```python
    if "source_popularity" in signal_codes and dl_count is not None:
        return _pick([
            "Free DL — grabbed {dl} times on {source_disp}.",
            "{dl} downloads on {source_disp} already.",
            "DJs are on this — {dl} downloads on {source_disp}.",
        ])
```

- [ ] **Step 4: Run reasons + report suites**

Run: `./venv/bin/pytest tests/test_reasons.py tests/test_report.py -v`
Expected: reasons PASS. If a report snapshot embeds a candidate that now matches the new branch
(has the signal + download_count), the snapshot diff appears here — inspect it; only accept
changes that are the new reason line, and update the frozen string deliberately in this commit.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/reasons.py tests/test_reasons.py tests/test_report.py
git commit -m "feat: popularity reason line with SoundCloud brand casing"
```

---

### Task 8: Report renderers — section in both Discord reports

**Files:**
- Modify: `src/pipeline/report.py` (`_SECTION_ORDER` line 24; weekly renderer after the
  Wildcards block ~line 340; mix-prep renderer after the Deep Cuts block)
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: sections dicts with `"free_downloads"` key (Tasks 5-6).
- Produces: `_SECTION_ORDER = ("top_picks", "label_watch", "artist_watch", "wildcards",
  "deep_cuts", "free_downloads")`; `## 🆓 Free Downloads` blocks in both renderers; continuous
  numbering through the section.

- [ ] **Step 1: Write the failing tests**

```python
def test_weekly_report_renders_free_downloads_section():
    sections = {
        "top_picks": [_candidate(title="Store Hit")],
        "free_downloads": [_candidate(title="Boot VIP", source="soundcloud")],
    }
    text = generate_report(sections, "2026-W29", {}, None)
    assert "## 🆓 Free Downloads" in text
    assert text.index("## 🆓 Free Downloads") > text.index("Store Hit")
    # numbering continues across sections: store track is 1., lane track is 2.
    assert "2." in text.split("## 🆓 Free Downloads")[1]


def test_mix_prep_report_renders_free_downloads_section():
    sections = {
        "deep_cuts": [_candidate(title="Cut")],
        "free_downloads": [_candidate(title="Boot", source="soundcloud")],
    }
    text = generate_mix_prep_report(sections, "mixprep-ukg-2026-07-17", {}, "ukg", None)
    assert "## 🆓 Free Downloads" in text
    assert text.index("## 🆓 Free Downloads") > text.index("## 🎧 Deep Cuts")


def test_empty_free_downloads_section_omitted():
    sections = {"top_picks": [_candidate(title="Only")], "free_downloads": []}
    text = generate_report(sections, "2026-W29", {}, None)
    assert "Free Downloads" not in text
```

(Adapt `_candidate` and the exact `generate_report`/`generate_mix_prep_report` signatures to what
`tests/test_report.py` already uses — the file has established fixtures for both renderers.)

- [ ] **Step 2: Run to verify they fail**

Run: `./venv/bin/pytest tests/test_report.py -k free_downloads -v`
Expected: FAIL — `report_order` raises `ValueError: unknown section keys {'free_downloads'}` (report.py:49).

- [ ] **Step 3: Implement**

(a) Line 24:

```python
_SECTION_ORDER = ("top_picks", "label_watch", "artist_watch", "wildcards", "deep_cuts", "free_downloads")
```

(b) Weekly renderer — after the Wildcards block (line ~340), before `recommended_count`:

```python
    # Free Downloads — exclusive lane (pipeline.free_download_sources)
    free_downloads = sections.get("free_downloads", [])
    if free_downloads:
        lines.append("## 🆓 Free Downloads")
        for c in free_downloads:
            lines.extend(_render_track(c))
        lines.append("")
```

(c) Mix-prep renderer — after the Deep Cuts block, before the footer:

```python
    free_downloads = sections.get("free_downloads", [])
    if free_downloads:
        lines.append("## 🆓 Free Downloads")
        for c in free_downloads:
            lines.extend(_render_track(c))
        lines.append("")
```

- [ ] **Step 4: Run the report suite; update snapshots deliberately**

Run: `./venv/bin/pytest tests/test_report.py -v`
Expected: new tests PASS; frozen snapshots must be byte-identical (their fixture sections have no
`free_downloads` key, and empty/absent keys render nothing). If a snapshot does change, the diff
must consist ONLY of the new section content — anything else is a regression; stop and fix.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/report.py tests/test_report.py
git commit -m "feat: render Free Downloads section in weekly and mix-prep reports"
```

---

### Task 9: Artifact unification + audition label + cross-renderer consistency test

**Files:**
- Modify: `src/pipeline/report_artifact.py` (`_SECTION_LABELS` lines 29-35; literal tuple line 114)
- Modify: `src/pipeline/audition.py` (`_SECTION_LABELS` lines 59-65)
- Test: `tests/test_report_artifact.py`

**Interfaces:**
- Consumes: `_SECTION_ORDER` from `src/pipeline/report.py` (Task 8's six-key tuple).
- Produces: artifact iterates the shared order; both label maps carry
  `"free_downloads": "Free Downloads"`; a consistency test locks all four paths together.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_report_artifact.py`)

```python
def test_artifact_includes_free_downloads_section():
    sections = {
        "top_picks": [_candidate(title="Store Hit")],
        "free_downloads": [_candidate(title="Boot VIP", source="soundcloud")],
    }
    artifact = _build(sections)
    keys = [s["key"] for s in artifact["sections"]]
    assert keys == ["top_picks", "free_downloads"]
    fd = artifact["sections"][1]
    assert fd["label"] == "Free Downloads"
    assert fd["tracks"][0]["track_no"] == 2  # numbering shared with report_order


def test_all_section_order_keys_render_in_every_path():
    """A section key present in _SECTION_ORDER must reach the Discord text, the
    artifact, and the audition page — guards against a renderer being missed."""
    from src.pipeline.audition import generate_audition_page
    from src.pipeline.report import _SECTION_ORDER, generate_report, generate_mix_prep_report
    weekly_keys = [k for k in _SECTION_ORDER if k != "deep_cuts"]
    sections = {k: [_candidate(title=f"T-{k}", artist=f"A-{k}")] for k in weekly_keys}
    text = generate_report(sections, "2026-W29", {}, None)
    artifact = _build(sections)
    page = generate_audition_page(sections, "2026-W29", None, profiles={}, label_artists={})
    for k in weekly_keys:
        assert f"T-{k}" in text, f"{k} missing from Discord report"
        assert f"T-{k}" in page, f"{k} missing from audition page"
    assert [s["key"] for s in artifact["sections"]] == weekly_keys
```

(Adapt `generate_report`/`generate_audition_page` call signatures to the fixtures already used in
`tests/test_report.py` / `tests/test_audition.py`.)

- [ ] **Step 2: Run to verify they fail**

Run: `./venv/bin/pytest tests/test_report_artifact.py -k "free_downloads or every_path" -v`
Expected: FAIL — the artifact's literal tuple omits `free_downloads`, so `keys == ["top_picks"]`.

- [ ] **Step 3: Implement**

(a) `report_artifact.py` — add to imports (the file already imports from `src.pipeline.report`):

```python
from src.pipeline.report import _SECTION_ORDER
```

Replace the literal tuple at line 114:

```python
    for section_key in _SECTION_ORDER:
```

Add to `_SECTION_LABELS` (lines 29-35):

```python
    "free_downloads": "Free Downloads",
```

(b) `audition.py` — add the same entry to its `_SECTION_LABELS` (lines 59-65):

```python
    "free_downloads": "Free Downloads",
```

- [ ] **Step 4: Run artifact + audition + web suites**

Run: `./venv/bin/pytest tests/test_report_artifact.py tests/test_audition.py tests/test_web_api.py -v`
Expected: all PASS (web schema's `ReportSection.key` is a free string — no schema work).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/report_artifact.py src/pipeline/audition.py tests/test_report_artifact.py
git commit -m "feat: free downloads section in artifact and audition page, unify section order"
```

---

### Task 10: settings.yaml + README

**Files:**
- Modify: `config/settings.yaml` (`pipeline:` block and `scoring:` block)
- Modify: `README.md` (configuration docs, scoring signals table, report sections description)

- [ ] **Step 1: settings.yaml — add under `pipeline:`** (after `section_min_score`)

```yaml
  # Free-download lane — sources listed here route to the exclusive
  # "Free Downloads" section and never compete with store sections.
  free_download_sources: [soundcloud]
  free_downloads_count: 5             # weekly slots
  mix_prep_free_downloads_count: 10   # mix-prep slots
  free_downloads_min_score: 0.0       # lane floor — 0 = best N of what the lane found
```

and under `scoring:` (after the mixupload popularity pair):

```yaml
  # SoundCloud popularity
  w_soundcloud_popularity: 0.25        # Bonus for SoundCloud tracks other DJs are downloading
  soundcloud_popularity_downloads: 50  # Minimum download count to earn the signal
```

- [ ] **Step 2: README — three edits**

(a) Configuration docs: document the four new `pipeline` keys with defaults, one bullet in the
configuration section alongside the existing pipeline-count bullets.

(b) Scoring signals table: new row

```markdown
| `source_popularity` (SoundCloud) | +0.25 | SoundCloud free-DL with ≥50 downloads — DJs are already grabbing it |
```

and correct the Mixupload row's note to state it fires **only for Mixupload tracks** (source-gated).

(c) "How it works" step 5 / report description: one sentence — free-download sources
(`pipeline.free_download_sources`) surface in an exclusive "🆓 Free Downloads" section in both
weekly and mix-prep reports rather than competing with store releases.

- [ ] **Step 3: Validate config loads**

Run: `./venv/bin/python -m tunefinder check-config`
Expected: validates clean, no unknown-scoring-key warnings.

- [ ] **Step 4: Commit**

```bash
git add config/settings.yaml README.md
git commit -m "feat: enable free-downloads lane in settings; document config and signals"
```

---

### Task 11: Full validation + dry runs

- [ ] **Step 1: Full suite**

Run: `./venv/bin/pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 2: Weekly dry run**

Run: `./venv/bin/python -m tunefinder run --dry-run`
Expected: log shows `free_downloads: N (lane floor=0.0)` with N > 0; report text contains
`## 🆓 Free Downloads` with SoundCloud tracks and popularity/genre reason lines. Paste the report
section and the funnel stats for review.

- [ ] **Step 3: Mix-prep dry run**

Run: `./venv/bin/python -m tunefinder mix-prep ukg --dry-run`
Expected: `## 🆓 Free Downloads` block after Deep Cuts with UKG lane items. Paste for review.

- [ ] **Step 4: Verify no live side effects**

`git status` — only intended files changed; `data/` untouched apart from normal dry-run reads.
Do NOT push, PR, or deploy in this task — report results and stop for review.
