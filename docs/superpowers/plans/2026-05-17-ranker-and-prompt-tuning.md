# Ranker and Prompt Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve weekly Discord report quality by tuning the deterministic ranker (catalog-augmented genres, scaled label/cross-source signals, artist-recency penalty, pool age penalty) and tightening Stage 1 / Stage 2 LLM prompts (richer payload, two-shot anchor, voice anti-patterns, per-call temperature override). Bootstrap a pytest suite covering the new behavior.

**Architecture:** No new runtime components, no new LLM calls. All ranker/prompt changes live in existing pipeline modules. Stage 1 reason enrichment gets a `profiles` parameter so it can quote real catalog facts. Stage 1 uses a per-call temperature override (label synopsis calls stay at the conservative default). One narrow `settings.yaml` change for Stage 2 temperature. Tests live in `tests/` mirroring `src/` layout and mock external IO (LLM HTTP calls, history file reads where convenient).

**Tech Stack:** Python 3, dataclasses, stdlib. New dev-time dependency: `pytest` (added to a new `requirements-dev.txt`). Tests use pytest fixtures and `monkeypatch` for stubbing out the LLM HTTP layer.

**Spec:** `docs/superpowers/specs/2026-05-17-ranker-and-prompt-tuning-design.md`

**Discipline:** TDD per task — write the failing test first, verify it fails, implement the minimum to pass, verify it passes, commit. Each commit leaves a green build (`pytest tests/ -v` exits 0).

---

## File Structure

Files created:
- `requirements-dev.txt` — pytest pin.
- `tests/__init__.py` — empty marker.
- `tests/conftest.py` — shared fixtures (tmp_path-based data_dir, sample profiles).
- `tests/test_models.py` — `Candidate.pool_added_at` default.
- `tests/test_pool.py` — `pool_to_candidates` carries `added_at`.
- `tests/test_history.py` — `recent_recommended_artists` (window, both files, splits).
- `tests/test_ranker.py` — augmented genres, scaled label, scaled cross-source, recency penalty, pool age penalty.
- `tests/test_report.py` — `_format_weekly_stats`, `_enrich_reasons` payload builder (mock `call_stage1`).
- `tests/test_llm.py` — `call_stage1` temperature override.

Files modified:
- `src/models.py` — add `Candidate.pool_added_at: Optional[str]`.
- `src/pipeline/pool.py` — populate `pool_added_at` in `pool_to_candidates`.
- `src/pipeline/history.py` — new `recent_recommended_artists` helper.
- `src/pipeline/ranker.py` — augmented genre set, scaled label/cross-source, recency penalty, pool age penalty, weight constants renamed.
- `src/llm.py` — `call_stage1` gains optional `temperature: float | None = None`.
- `src/pipeline/report.py` — `_enrich_reasons` payload + prompt rewrite + two-shot + per-call temp, Stage 2 stats injection + anti-patterns. `generate_report` / `generate_mix_prep_report` gain `profiles`.
- `tunefinder/__main__.py` — pass `profiles` to `generate_report` / `generate_mix_prep_report`.
- `config/settings.yaml` — Stage 2 temperature 0.2 → 0.3.

Files NOT modified: fetchers, dedup, label_cache, output/discord, config.py, logger.py.

**Note:** `CLAUDE.md` was updated separately (outside the plan tasks) to relax the script-style framing and permit dev dependencies in `requirements-dev.txt`. Already committed/staged before plan execution starts.

---

### Task 0: Bootstrap pytest

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Create `requirements-dev.txt`**

> Adding `pytest` is a new dev-time dependency. The user pre-approved this during brainstorming (chose pytest over stdlib unittest). The updated `CLAUDE.md` also permits dev deps in `requirements-dev.txt` without re-asking.

```
pytest>=8.0
```

- [ ] **Step 2: Install pytest into the existing venv**

Run:
```bash
./venv/bin/pip install -r requirements-dev.txt
```
Expected: pytest installed without error. Confirm with `./venv/bin/pytest --version`.

- [ ] **Step 3: Create `tests/__init__.py`**

Empty file. Just `touch`.

```bash
: > tests/__init__.py
```

- [ ] **Step 4: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures."""
import os
import sys

# Make `src.*` and `tunefinder.*` imports work when pytest is launched from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.models import ArtistProfile


@pytest.fixture
def sample_profiles():
    """A small profile dict used across ranker/report tests."""
    return {
        "Sully": ArtistProfile(
            name="Sully",
            play_count=4,
            genres_seen=["breaks", "uk-bass"],
            track_titles=["Swandive", "Glasshouse", "Cherry"],
        ),
        "Skee Mask": ArtistProfile(
            name="Skee Mask",
            play_count=2,
            genres_seen=["electronica", "breaks"],
            track_titles=["Rio Dembo"],
        ),
        "Calibre": ArtistProfile(
            name="Calibre",
            play_count=6,
            genres_seen=["dnb"],
            track_titles=["Mr Right On"],
        ),
    }
```

- [ ] **Step 5: Create `tests/test_smoke.py`**

```python
def test_smoke_imports():
    """If src modules import cleanly, the test harness is wired up."""
    from src.models import Candidate
    from src.pipeline.ranker import rank_candidates
    from src.pipeline.report import generate_report
    from src.llm import call_stage1
    assert callable(rank_candidates)
    assert callable(generate_report)
    assert callable(call_stage1)
    assert Candidate
```

- [ ] **Step 6: Verify suite runs and the smoke test passes**

Run:
```bash
./venv/bin/pytest tests/ -v
```
Expected: 1 test collected, `PASSED`.

- [ ] **Step 7: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py tests/test_smoke.py
git commit -m "test: bootstrap pytest with smoke test and shared fixtures"
```

---

### Task 1: `Candidate.pool_added_at` field

**Files:**
- Create: `tests/test_models.py`
- Modify: `src/models.py:91-113`

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:

```python
from src.models import Candidate


def test_candidate_pool_added_at_defaults_to_none():
    c = Candidate(artist="x", title="y", link="", source="test")
    assert c.pool_added_at is None


def test_candidate_pool_added_at_accepts_iso_string():
    c = Candidate(artist="x", title="y", link="", source="test",
                  pool_added_at="2026-04-01T00:00:00+00:00")
    assert c.pool_added_at == "2026-04-01T00:00:00+00:00"
```

- [ ] **Step 2: Run the test — expect failure**

Run:
```bash
./venv/bin/pytest tests/test_models.py -v
```
Expected: `FAILED` — `TypeError: Candidate.__init__() got an unexpected keyword argument 'pool_added_at'` (or `AttributeError` on the first test).

- [ ] **Step 3: Add the field to `Candidate`**

In `src/models.py`, in the `Candidate` dataclass, append after `raw_metadata`:

```python
    pool_added_at: Optional[str] = None
```

- [ ] **Step 4: Run the test — expect pass**

Run:
```bash
./venv/bin/pytest tests/test_models.py -v
```
Expected: 2 tests `PASSED`.

- [ ] **Step 5: Run the full suite**

Run:
```bash
./venv/bin/pytest tests/ -v
```
Expected: all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/models.py tests/test_models.py
git commit -m "feat(models): add Candidate.pool_added_at for pool age tracking"
```

---

### Task 2: `pool_to_candidates` carries `added_at`

**Files:**
- Create: `tests/test_pool.py`
- Modify: `src/pipeline/pool.py:84-99`

- [ ] **Step 1: Write the failing test**

`tests/test_pool.py`:

```python
from src.models import PoolRecord
from src.pipeline.pool import pool_to_candidates


def test_pool_to_candidates_carries_added_at():
    rec = PoolRecord(
        artist="a", title="t", link="", source="s",
        added_at="2026-04-01T00:00:00+00:00",
    )
    candidates = pool_to_candidates([rec])
    assert len(candidates) == 1
    assert candidates[0].pool_added_at == "2026-04-01T00:00:00+00:00"


def test_pool_to_candidates_handles_missing_added_at():
    rec = PoolRecord(artist="a", title="t", link="", source="s", added_at="")
    candidates = pool_to_candidates([rec])
    assert candidates[0].pool_added_at is None
```

- [ ] **Step 2: Run the test — expect failure**

Run:
```bash
./venv/bin/pytest tests/test_pool.py -v
```
Expected: both tests FAIL (current `pool_to_candidates` doesn't set `pool_added_at`).

- [ ] **Step 3: Update `pool_to_candidates`**

In `src/pipeline/pool.py`, replace the function with:

```python
def pool_to_candidates(records: list[PoolRecord]) -> list[Candidate]:
    """Convert pool records to Candidate objects ready for scoring (score=0, signals=[]).
    Carries `added_at` onto `Candidate.pool_added_at` so the ranker can apply an age penalty.
    """
    return [
        Candidate(
            artist=r.artist,
            title=r.title,
            link=r.link,
            source=r.source,
            label=r.label,
            release_date=r.release_date,
            release_name=r.release_name,
            genre_tags=r.genre_tags,
            raw_metadata=r.raw_metadata,
            pool_added_at=r.added_at or None,
        )
        for r in records
    ]
```

- [ ] **Step 4: Run the test — expect pass**

Run:
```bash
./venv/bin/pytest tests/test_pool.py -v
```
Expected: 2 tests `PASSED`.

- [ ] **Step 5: Run the full suite**

Run:
```bash
./venv/bin/pytest tests/ -v
```
Expected: all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/pool.py tests/test_pool.py
git commit -m "feat(pool): carry added_at onto Candidate.pool_added_at"
```

---

### Task 3: `recent_recommended_artists` helper

**Files:**
- Create: `tests/test_history.py`
- Modify: `src/pipeline/history.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_history.py`:

```python
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.pipeline.history import recent_recommended_artists


def _write_history(path, records):
    path.write_text(json.dumps(records))


def _rec(artist, days_ago, mix_prep=False):
    return {
        "artist": artist,
        "title": "t",
        "link": "",
        "source": "s",
        "recommended_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
        "report_id": "test",
    }


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def test_recency_includes_within_window(data_dir):
    _write_history(data_dir / "recommendation_history.json", [_rec("Sully", days_ago=3)])
    _write_history(data_dir / "mix_prep_history.json", [])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "sully" in result  # normalised lower


def test_recency_excludes_outside_window(data_dir):
    _write_history(data_dir / "recommendation_history.json", [_rec("Sully", days_ago=60)])
    _write_history(data_dir / "mix_prep_history.json", [])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "sully" not in result


def test_recency_includes_mix_prep_history(data_dir):
    _write_history(data_dir / "recommendation_history.json", [])
    _write_history(data_dir / "mix_prep_history.json", [_rec("Calibre", days_ago=2)])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "calibre" in result


def test_recency_splits_collab_artists(data_dir):
    _write_history(data_dir / "recommendation_history.json",
                   [_rec("Bakey, Kasia", days_ago=1)])
    _write_history(data_dir / "mix_prep_history.json", [])
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert "bakey" in result
    assert "kasia" in result


def test_recency_handles_missing_files(data_dir):
    # Files do not exist — should return empty set without crashing.
    result = recent_recommended_artists(str(data_dir), weeks=4)
    assert result == set()
```

- [ ] **Step 2: Run the tests — expect failure**

Run:
```bash
./venv/bin/pytest tests/test_history.py -v
```
Expected: `ImportError` on `recent_recommended_artists`.

- [ ] **Step 3: Implement the helper**

Append to `src/pipeline/history.py`:

```python
# ---------------------------------------------------------------------------
# Artist-level recency lookup
# ---------------------------------------------------------------------------

def recent_recommended_artists(data_dir: str, weeks: int = 4) -> set[str]:
    """Return normalised artist strings recommended within the last `weeks` weeks
    across BOTH weekly history (recommendation_history.json) and mix-prep history
    (mix_prep_history.json). Both represent tracks the DJ already saw — both
    should suppress repeats at the artist level.

    Each record's artist string is split into individual artists (handles
    "A, B" / "A feat. B" / "A & B" / "A x B") and normalised via dedup.
    """
    from datetime import datetime, timedelta, timezone
    from src.pipeline.dedup import normalise_artist
    from src.pipeline.profile import _split_artists

    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    records = load_history(data_dir) + load_mix_prep_history(data_dir)

    recent: set[str] = set()
    for r in records:
        if not r.recommended_at:
            continue
        try:
            ts = datetime.fromisoformat(r.recommended_at)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        for part in _split_artists(r.artist):
            recent.add(normalise_artist(part))

    logger.info(f"[history] {len(recent)} artists in {weeks}-week recency window")
    return recent
```

- [ ] **Step 4: Run the tests — expect pass**

Run:
```bash
./venv/bin/pytest tests/test_history.py -v
```
Expected: 5 tests `PASSED`.

- [ ] **Step 5: Run the full suite**

Run:
```bash
./venv/bin/pytest tests/ -v
```
Expected: all tests green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/history.py tests/test_history.py
git commit -m "feat(history): add recent_recommended_artists helper for ranker"
```

---

### Task 4: Ranker — catalog-augmented genre set

**Files:**
- Create: `tests/test_ranker.py` (first test class — extended in later tasks)
- Modify: `src/pipeline/ranker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ranker.py`:

```python
from src.models import ArtistProfile, Candidate
from src.pipeline.ranker import _build_genre_set


def test_genre_set_includes_baseline():
    gs = _build_genre_set({})
    for g in {"dnb", "breaks", "uk-bass", "ukg", "house", "techno", "electronica", "electronic"}:
        assert g in gs


def test_genre_set_augments_from_profiles_when_threshold_met():
    # 3 distinct profiles all tagged "ambient" — should land in the set.
    profiles_lower = {
        "a": ArtistProfile(name="A", genres_seen=["ambient"]),
        "b": ArtistProfile(name="B", genres_seen=["ambient"]),
        "c": ArtistProfile(name="C", genres_seen=["ambient"]),
    }
    gs = _build_genre_set(profiles_lower)
    assert "ambient" in gs


def test_genre_set_skips_below_threshold():
    # Only 2 profiles tagged "industrial" — below threshold of 3.
    profiles_lower = {
        "a": ArtistProfile(name="A", genres_seen=["industrial"]),
        "b": ArtistProfile(name="B", genres_seen=["industrial"]),
    }
    gs = _build_genre_set(profiles_lower)
    assert "industrial" not in gs
```

- [ ] **Step 2: Run the tests — expect failure**

Run:
```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: `ImportError` on `_build_genre_set`.

- [ ] **Step 3: Implement `_build_genre_set` and thread it through**

In `src/pipeline/ranker.py`:

(a) Replace the module-level `_OUR_GENRES = {...}` constant with:

```python
_GENRE_AUGMENT_MIN_ARTISTS = 3

_BASELINE_GENRES = {"dnb", "breaks", "uk-bass", "ukg", "house", "techno", "electronica", "electronic"}


def _build_genre_set(profiles_lower: dict[str, ArtistProfile]) -> set[str]:
    """Return the curated baseline genres unioned with any catalog genre that
    appears across `_GENRE_AUGMENT_MIN_ARTISTS` or more distinct profiles.
    """
    counts: dict[str, int] = {}
    for profile in profiles_lower.values():
        for g in profile.genres_seen:
            counts[g] = counts.get(g, 0) + 1
    augmented = {g for g, n in counts.items() if n >= _GENRE_AUGMENT_MIN_ARTISTS}
    result = _BASELINE_GENRES | augmented
    logger.info(f"[ranker] Genre set: {len(_BASELINE_GENRES)} baseline + {len(augmented - _BASELINE_GENRES)} catalog-augmented")
    return result
```

(b) Update `_score` signature to take `genres_set: set[str]`:

```python
def _score(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    relevant_labels: set[str],
    genres_set: set[str],
) -> None:
```

Replace `[g for g in c.genre_tags if g in _OUR_GENRES]` with `[g for g in c.genre_tags if g in genres_set]`.

(c) Update `_assign_sections` to accept `genres_set` and use it where `_OUR_GENRES` was referenced:

```python
def _assign_sections(
    ranked: list[Candidate],
    settings,
    genres_set: set[str],
) -> dict[str, list[Candidate]]:
```

Replace `next((g for g in c.genre_tags if g in _OUR_GENRES and g not in _UNCAPPED_GENRES), None)` with `next((g for g in c.genre_tags if g in genres_set and g not in _UNCAPPED_GENRES), None)`.

(d) Update `rank_candidates` to build the set and thread it:

```python
def rank_candidates(
    candidates: list[Candidate],
    profiles: dict[str, ArtistProfile],
    settings,
    label_seed: list[Candidate] | None = None,
) -> dict[str, list[Candidate]]:
    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    genres_set = _build_genre_set(profiles_lower)
    relevant_labels = _build_relevant_labels(label_seed if label_seed is not None else candidates, profiles_lower)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels, genres_set)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections(ranked, settings, genres_set)
```

(e) Update `rank_candidates_mix_prep` to thread the set into `_score` only (mix-prep section assigner has no genre cap):

```python
def rank_candidates_mix_prep(
    candidates: list[Candidate],
    profiles: dict[str, ArtistProfile],
    settings,
    label_seed: list[Candidate] | None = None,
) -> dict[str, list[Candidate]]:
    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    genres_set = _build_genre_set(profiles_lower)
    relevant_labels = _build_relevant_labels(label_seed if label_seed is not None else candidates, profiles_lower)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels, genres_set)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Mix-prep scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections_mix_prep(ranked, settings)
```

- [ ] **Step 4: Run the tests — expect pass**

Run:
```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: 3 tests `PASSED`.

- [ ] **Step 5: Run full suite**

Run:
```bash
./venv/bin/pytest tests/ -v
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat(ranker): derive genre set from catalog plus curated baseline"
```

---

### Task 5: Ranker — scaled label signal

**Files:**
- Modify: `tests/test_ranker.py` (append tests)
- Modify: `src/pipeline/ranker.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_ranker.py`:

```python
from src.pipeline.ranker import _build_relevant_labels, _score, _build_genre_set


def _candidate(artist="A", title="T", label=None, source="s", **kw):
    return Candidate(artist=artist, title=title, link="", source=source, label=label, **kw)


def test_label_signal_scales_with_known_artist_count():
    profiles_lower = {
        "sully": ArtistProfile(name="Sully"),
        "skee mask": ArtistProfile(name="Skee Mask"),
        "calibre": ArtistProfile(name="Calibre"),
    }
    # Three known artists on Ilian Tape.
    candidates = [
        _candidate(artist="Sully", label="Ilian Tape"),
        _candidate(artist="Skee Mask", label="Ilian Tape"),
        _candidate(artist="Calibre", label="Ilian Tape"),
    ]
    _, counts = _build_relevant_labels(candidates, profiles_lower)
    assert counts["ilian tape"] == 3

    target = _candidate(artist="Unknown", title="T", label="Ilian Tape")
    _score(target, profiles_lower, {"ilian tape"}, counts, _build_genre_set(profiles_lower))
    # 1.5 base + 0.5 * min(3, cap=3) = 3.0
    assert target.score == 3.0


def test_label_signal_base_when_one_known_artist():
    profiles_lower = {"sully": ArtistProfile(name="Sully")}
    candidates = [_candidate(artist="Sully", label="Astrophonica")]
    _, counts = _build_relevant_labels(candidates, profiles_lower)
    target = _candidate(artist="Other", title="T", label="Astrophonica")
    _score(target, profiles_lower, {"astrophonica"}, counts, _build_genre_set(profiles_lower))
    # 1.5 base + 0.5 * 1 = 2.0
    assert target.score == 2.0


def test_label_signal_caps_at_three_artists():
    profiles_lower = {f"a{i}": ArtistProfile(name=f"A{i}") for i in range(5)}
    candidates = [_candidate(artist=f"A{i}", label="Big Label") for i in range(5)]
    _, counts = _build_relevant_labels(candidates, profiles_lower)
    target = _candidate(artist="X", title="T", label="Big Label")
    _score(target, profiles_lower, {"big label"}, counts, _build_genre_set(profiles_lower))
    # cap of 3 known artists → 1.5 + 0.5*3 = 3.0
    assert target.score == 3.0
```

- [ ] **Step 2: Run tests — expect failure**

Run:
```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: the three new tests FAIL — `_build_relevant_labels` currently returns only a set, and `_score` doesn't take a counts dict.

- [ ] **Step 3: Implement scaled label signal**

In `src/pipeline/ranker.py`:

(a) Replace `_W_LABEL_MATCH = 2.5` with:

```python
_W_LABEL_BASE = 1.5
_W_LABEL_PER_ARTIST = 0.5
_LABEL_ARTIST_CAP = 3
```

(b) Replace `_build_relevant_labels`:

```python
def _build_relevant_labels(
    candidates: list[Candidate],
    profiles_lower: dict[str, ArtistProfile],
) -> tuple[set[str], dict[str, int]]:
    """Return (relevant_labels, label_known_artist_counts)."""
    relevant: set[str] = set()
    counts: dict[str, set[str]] = {}
    for c in candidates:
        if not c.label:
            continue
        label_key = c.label.lower().strip()
        for part in _split_artists(c.artist):
            profile = profiles_lower.get(part.lower().strip())
            if profile:
                relevant.add(label_key)
                counts.setdefault(label_key, set()).add(profile.name.lower())
    counts_int = {k: len(v) for k, v in counts.items()}
    logger.info(f"[ranker] {len(relevant)} relevant labels derived from candidate set")
    return relevant, counts_int
```

(c) Update `_score` signature to accept the counts:

```python
def _score(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    relevant_labels: set[str],
    label_artist_counts: dict[str, int],
    genres_set: set[str],
) -> None:
```

(d) Replace the label-signal block in `_score`:

```python
    # --- Label signal ---
    if c.label and c.label.lower().strip() in relevant_labels:
        label_key = c.label.lower().strip()
        known_on_label = min(label_artist_counts.get(label_key, 1), _LABEL_ARTIST_CAP)
        label_bonus = _W_LABEL_BASE + _W_LABEL_PER_ARTIST * known_on_label
        score += label_bonus
        c.signals.append(RecommendationSignal(
            code="label_match",
            explanation=f"{c.label} — a label you've played artists from.",
        ))
```

(e) Update both `rank_candidates` and `rank_candidates_mix_prep` to unpack the tuple and pass the counts:

```python
    relevant_labels, label_artist_counts = _build_relevant_labels(
        label_seed if label_seed is not None else candidates, profiles_lower
    )

    for c in candidates:
        _score(c, profiles_lower, relevant_labels, label_artist_counts, genres_set)
```

- [ ] **Step 4: Run tests — expect pass**

Run:
```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: all ranker tests `PASSED`.

- [ ] **Step 5: Run full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat(ranker): scale label match by number of known artists on label"
```

---

### Task 6: Ranker — scaled cross-source signal

**Files:**
- Modify: `tests/test_ranker.py` (append)
- Modify: `src/pipeline/ranker.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_ranker.py`:

```python
def test_cross_source_two_sources_scores_1_point_0():
    c = _candidate(raw_metadata={"seen_on_sources": ["a", "b"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    # 0.5 * min(2, 4) = 1.0
    assert c.score == 1.0


def test_cross_source_three_sources_scores_1_point_5():
    c = _candidate(raw_metadata={"seen_on_sources": ["a", "b", "c"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 1.5


def test_cross_source_caps_at_four():
    c = _candidate(raw_metadata={"seen_on_sources": ["a", "b", "c", "d", "e"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    # capped at 4 → 0.5 * 4 = 2.0
    assert c.score == 2.0


def test_cross_source_one_source_no_bonus():
    c = _candidate(raw_metadata={"seen_on_sources": ["a"]})
    _score(c, {}, set(), {}, _build_genre_set({}))
    assert c.score == 0.0
```

- [ ] **Step 2: Run tests — expect failure**

Expected: scaling math doesn't match yet (currently +1.0 flat for any `>=2`).

- [ ] **Step 3: Implement scaled cross-source**

In `src/pipeline/ranker.py`:

(a) Replace `_W_CROSS_SOURCE = 1.0` with:

```python
_W_CROSS_SOURCE_PER = 0.5
_CROSS_SOURCE_CAP = 4
```

(b) Replace the cross-source block in `_score`:

```python
    # --- Cross-source credibility ---
    seen_on = c.raw_metadata.get("seen_on_sources", [c.source])
    if len(seen_on) >= 2:
        capped = min(len(seen_on), _CROSS_SOURCE_CAP)
        score += _W_CROSS_SOURCE_PER * capped
        c.signals.append(RecommendationSignal(
            code="cross_source",
            explanation=f"Flagged by {len(seen_on)} sources: {', '.join(seen_on)}.",
        ))
```

- [ ] **Step 4: Run tests — expect pass**

```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: green.

- [ ] **Step 5: Run full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat(ranker): scale cross-source bonus by source count"
```

---

### Task 7: Ranker — artist-recency penalty

**Files:**
- Modify: `tests/test_ranker.py` (append)
- Modify: `src/pipeline/ranker.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_ranker.py`:

```python
def test_recency_penalty_applied_when_matched_artist_in_recent_set():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    # Pre-fill the score we expect by going through _score directly.
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), recent_artists={"sully"})
    # known_artist: 1 * 3.0 = 3.0
    # recency penalty: -0.75
    # = 2.25
    assert c.score == 2.25


def test_recency_penalty_skipped_when_artist_not_recent():
    profiles_lower = {"sully": ArtistProfile(name="Sully", play_count=1)}
    c = _candidate(artist="Sully")
    _score(c, profiles_lower, set(), {}, _build_genre_set(profiles_lower), recent_artists=set())
    assert c.score == 3.0


def test_recency_penalty_skipped_when_no_known_artist_match():
    c = _candidate(artist="Unknown")
    _score(c, {}, set(), {}, _build_genre_set({}), recent_artists={"some-other-artist"})
    # No known_artist signal triggered → penalty branch never reached.
    assert c.score == 0.0
```

- [ ] **Step 2: Run tests — expect failure**

Expected: `_score` doesn't accept `recent_artists` parameter yet.

- [ ] **Step 3: Implement recency penalty**

In `src/pipeline/ranker.py`:

(a) Add to the weight constants:

```python
_W_RECENCY_PENALTY = 0.75
_RECENCY_WEEKS = 4
```

(b) Update `_score` signature. **The new param has a default of `frozenset()` so the test calls written in Tasks 5 and 6 (which don't pass `recent_artists`) continue to compile and stay green.** `frozenset()` is safe as a mutable default because it's immutable — no shared-state hazard. Then add the penalty inside the `if matched:` branch (right after the existing `recurring_artist` block):

```python
def _score(
    c: Candidate,
    profiles_lower: dict[str, ArtistProfile],
    relevant_labels: set[str],
    label_artist_counts: dict[str, int],
    genres_set: set[str],
    recent_artists: frozenset[str] | set[str] = frozenset(),
) -> None:
```

Inside, after the `recurring_artist` block:

```python
        # --- Artist-recency penalty ---
        if any(normalise_artist(name) in recent_artists for name in matched):
            score -= _W_RECENCY_PENALTY
            c.signals.append(RecommendationSignal(
                code="recent_recommendation",
                explanation=f"{matched[0]} appeared in a recent report — soft down-weight.",
            ))
```

(c) Update both `rank_candidates` and `rank_candidates_mix_prep` to load and thread the recent set:

```python
def rank_candidates(
    candidates: list[Candidate],
    profiles: dict[str, ArtistProfile],
    settings,
    label_seed: list[Candidate] | None = None,
) -> dict[str, list[Candidate]]:
    from src.pipeline.history import recent_recommended_artists

    profiles_lower = {k.lower(): v for k, v in profiles.items()}
    genres_set = _build_genre_set(profiles_lower)
    relevant_labels, label_artist_counts = _build_relevant_labels(
        label_seed if label_seed is not None else candidates, profiles_lower
    )
    recent_artists = recent_recommended_artists(settings.data_dir, weeks=_RECENCY_WEEKS)

    for c in candidates:
        _score(c, profiles_lower, relevant_labels, label_artist_counts, genres_set, recent_artists)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
    logger.info(f"[ranker] Scored {len(ranked)} candidates — top score: {ranked[0].score if ranked else 0}")

    return _assign_sections(ranked, settings, genres_set)
```

Mirror in `rank_candidates_mix_prep` (same six lines added, identical except for the `_assign_sections_mix_prep` call at the end and the mix-prep log prefix).

- [ ] **Step 4: Run tests — expect pass**

```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: green.

- [ ] **Step 5: Run full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat(ranker): soft penalty for artists recommended in last 4 weeks"
```

---

### Task 8: Ranker — pool age penalty

**Files:**
- Modify: `tests/test_ranker.py` (append)
- Modify: `src/pipeline/ranker.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_ranker.py`:

```python
from datetime import datetime, timedelta, timezone


def test_pool_age_penalty_zero_weeks_no_subtraction():
    c = _candidate(pool_added_at=datetime.now(timezone.utc).isoformat())
    _score(c, {}, set(), {}, _build_genre_set({}), set())
    assert c.score == 0.0


def test_pool_age_penalty_three_weeks():
    added = (datetime.now(timezone.utc) - timedelta(weeks=3)).isoformat()
    c = _candidate(pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}), set())
    # 0.25 * 3 = 0.75 penalty
    assert c.score == -0.75


def test_pool_age_penalty_caps_at_negative_1_point_5():
    added = (datetime.now(timezone.utc) - timedelta(weeks=20)).isoformat()
    c = _candidate(pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}), set())
    assert c.score == -1.5


def test_pool_age_penalty_clamped_for_future_timestamp():
    added = (datetime.now(timezone.utc) + timedelta(weeks=5)).isoformat()
    c = _candidate(pool_added_at=added)
    _score(c, {}, set(), {}, _build_genre_set({}), set())
    # max(0, ...) clamp prevents negative weeks → no penalty
    assert c.score == 0.0


def test_pool_age_penalty_handles_bad_iso_string():
    c = _candidate(pool_added_at="not-a-date")
    _score(c, {}, set(), {}, _build_genre_set({}), set())
    assert c.score == 0.0
```

- [ ] **Step 2: Run tests — expect failure**

Expected: pool age branch not implemented yet.

- [ ] **Step 3: Implement pool age penalty**

In `src/pipeline/ranker.py`:

(a) Add to weight constants:

```python
_W_POOL_AGE_PER_WEEK = 0.25
_POOL_AGE_PENALTY_MAX = 1.5
```

(b) Inside `_score`, just before `c.score = round(score, 2)`, insert:

```python
    # --- Pool age penalty ---
    if c.pool_added_at:
        try:
            added = datetime.fromisoformat(c.pool_added_at)
            if added.tzinfo is None:
                added = added.replace(tzinfo=timezone.utc)
            days_old = (datetime.now(timezone.utc) - added).days
            weeks_old = max(0, days_old // 7)
            penalty = min(_W_POOL_AGE_PER_WEEK * weeks_old, _POOL_AGE_PENALTY_MAX)
            if penalty > 0:
                score -= penalty
                c.signals.append(RecommendationSignal(
                    code="pool_age",
                    explanation=f"Carried over from pool for {weeks_old} week{'s' if weeks_old != 1 else ''}.",
                ))
        except ValueError:
            pass
```

- [ ] **Step 4: Run tests — expect pass**

```bash
./venv/bin/pytest tests/test_ranker.py -v
```
Expected: green.

- [ ] **Step 5: Run full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/ranker.py tests/test_ranker.py
git commit -m "feat(ranker): apply weekly age penalty to pool-carried candidates"
```

---

### Task 9: `call_stage1` temperature override

**Files:**
- Create: `tests/test_llm.py`
- Modify: `src/llm.py:88-147`

- [ ] **Step 1: Write failing test**

`tests/test_llm.py`:

```python
import types

import pytest

import src.llm as llm_mod
from src.llm import call_stage1


class _FakeSettings:
    def __init__(self, temp=0.1):
        self.llm_stage1 = {
            "provider": "mistral",
            "model": "mistral-small",
            "temperature": temp,
            "max_tokens": 100,
            "timeout_seconds": 5,
        }
        self.llm_fallback_chain = []
        self.mistral_api_key = "test-key"


def test_call_stage1_uses_config_temperature_when_no_override(monkeypatch):
    captured = {}

    def fake_call(base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, path, extra_headers):
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(llm_mod, "_call_openai_compat", fake_call)
    call_stage1("p", "s", _FakeSettings(temp=0.1))
    assert captured["temperature"] == 0.1


def test_call_stage1_override_wins_over_config(monkeypatch):
    captured = {}

    def fake_call(base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, path, extra_headers):
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(llm_mod, "_call_openai_compat", fake_call)
    call_stage1("p", "s", _FakeSettings(temp=0.1), temperature=0.3)
    assert captured["temperature"] == 0.3


def test_call_stage1_override_zero_is_respected(monkeypatch):
    captured = {}

    def fake_call(base_url, api_key, model, system, prompt, temperature, max_tokens, timeout, path, extra_headers):
        captured["temperature"] = temperature
        return "[]"

    monkeypatch.setattr(llm_mod, "_call_openai_compat", fake_call)
    call_stage1("p", "s", _FakeSettings(temp=0.5), temperature=0.0)
    # 0.0 is a valid override and must beat the config 0.5.
    assert captured["temperature"] == 0.0
```

- [ ] **Step 2: Run test — expect failure**

Expected: `TypeError: call_stage1() got an unexpected keyword argument 'temperature'`.

- [ ] **Step 3: Implement the override**

In `src/llm.py`:

```python
def call_stage1(prompt: str, system: str, settings, temperature: float | None = None) -> str:
    """
    Cheap, fast extraction/classification via cascade chain.
    Chain order: llm.stage1 (primary) then each entry in llm.fallback_chain.
    Providers with no API key are silently skipped.
    Sleeps 1s between failures. Raises RuntimeError if all exhausted.

    If `temperature` is provided, it overrides the config value for this call only.
    """
    stage1_cfg = settings.llm_stage1
    effective_temperature = temperature if temperature is not None else stage1_cfg.get("temperature", 0.1)
    max_tokens = stage1_cfg.get("max_tokens", 4096)
    timeout = stage1_cfg.get("timeout_seconds", 60)
```

Then in the `_call_openai_compat` call inside the chain loop, pass `temperature=effective_temperature`.

- [ ] **Step 4: Run test — expect pass**

```bash
./venv/bin/pytest tests/test_llm.py -v
```
Expected: 3 tests green.

- [ ] **Step 5: Full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/llm.py tests/test_llm.py
git commit -m "feat(llm): add optional per-call temperature override to call_stage1"
```

---

### Task 10: Thread `profiles` through `generate_report` and `generate_mix_prep_report`

**Files:**
- Modify: `src/pipeline/report.py`
- Modify: `tunefinder/__main__.py:198, 366`

Plumbing only — no behavior change. No new tests in this task; coverage comes in Task 11 once `_enrich_reasons` actually uses `profiles`.

- [ ] **Step 1: Add `ArtistProfile` import in `report.py`**

In `src/pipeline/report.py`, replace `from src.models import Candidate` with:

```python
from src.models import ArtistProfile, Candidate
```

- [ ] **Step 2: Add `profiles` param to `_enrich_reasons`, `generate_report`, `generate_mix_prep_report`**

For `_enrich_reasons`, change the signature only (body unchanged for now):

```python
def _enrich_reasons(
    candidates: list[Candidate],
    settings,
    profiles: dict[str, ArtistProfile] | None = None,
) -> dict[str, str]:
```

For `generate_report`:

```python
def generate_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    settings,
    profiles: dict[str, ArtistProfile] | None = None,
) -> str:
```

Update its internal call:

```python
    reasons = _enrich_reasons(all_candidates, settings, profiles=profiles) if all_candidates else {}
```

For `generate_mix_prep_report`:

```python
def generate_mix_prep_report(
    sections: dict[str, list[Candidate]],
    report_id: str,
    stats: dict,
    genre: str,
    settings,
    profiles: dict[str, ArtistProfile] | None = None,
) -> str:
```

Update its internal call:

```python
    reasons = _enrich_reasons(all_candidates, settings, profiles=profiles) if all_candidates else {}
```

- [ ] **Step 3: Update `__main__.py` callers**

In `tunefinder/__main__.py`, locate the call near line 198:

```python
    report_text = generate_report(sections, report_id, stats, settings)
```

Change to:

```python
    report_text = generate_report(sections, report_id, stats, settings, profiles=profiles)
```

Locate the call near line 366:

```python
    report_text = generate_mix_prep_report(sections, report_id, stats, genre, settings)
```

Change to:

```python
    report_text = generate_mix_prep_report(sections, report_id, stats, genre, settings, profiles=profiles)
```

- [ ] **Step 4: Run full suite — should still be green**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green (no new tests, no regressions).

- [ ] **Step 5: Check-config**

```bash
./venv/bin/python -m tunefinder check-config
```
Expected: validation passes.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/report.py tunefinder/__main__.py
git commit -m "refactor(report): thread profiles into _enrich_reasons via generate_report"
```

---

### Task 11: Stage 1 payload rewrite + new prompt + two-shot + per-call temp

**Files:**
- Modify: `tests/test_report.py` (create)
- Modify: `src/pipeline/report.py`

To make `_enrich_reasons` testable without hitting the LLM, the test mocks `call_stage1` and captures the prompt/payload that would have been sent.

- [ ] **Step 1: Write failing tests**

Create `tests/test_report.py`:

```python
import json

import pytest

import src.pipeline.report as report_mod
from src.models import ArtistProfile, Candidate, RecommendationSignal


class _Settings:
    def __init__(self):
        self.data_dir = "data"


def _make_candidate(artist="Sully", title="Cherry", signals=None, **kw):
    return Candidate(
        artist=artist, title=title, link="", source="beatport",
        signals=signals or [],
        **kw,
    )


def test_enrich_reasons_includes_prior_titles_for_known_artist(monkeypatch, sample_profiles):
    captured = {}

    def fake_call_stage1(prompt, system, settings, temperature=None):
        captured["prompt"] = prompt
        captured["temperature"] = temperature
        # Echo back an empty result; we're only checking what we sent.
        return "[]"

    monkeypatch.setattr(report_mod, "call_stage1", fake_call_stage1)

    cand = _make_candidate(
        artist="Sully",
        title="Cherry",
        label="Astrophonica",
        genre_tags=["breaks"],
        raw_metadata={"chart_position": 3, "seen_on_sources": ["beatport"]},
        signals=[RecommendationSignal(code="known_artist", explanation="x")],
    )
    report_mod._enrich_reasons([cand], _Settings(), profiles=sample_profiles)

    assert captured["temperature"] == 0.3, "Stage 1 reason calls must override temp to 0.3"
    # Prior titles from Sully's profile (Swandive, Glasshouse) appear in the prompt.
    assert "Swandive" in captured["prompt"]
    assert "Glasshouse" in captured["prompt"]
    # The track itself is NOT in prior_titles_sample (we exclude it).
    payload_marker = '"prior_titles_sample"'
    assert payload_marker in captured["prompt"]


def test_enrich_reasons_empty_prior_titles_for_unknown_artist(monkeypatch):
    captured = {}
    monkeypatch.setattr(report_mod, "call_stage1",
                        lambda p, s, st, temperature=None: captured.setdefault("prompt", p) or "[]")
    cand = _make_candidate(
        artist="Nobody",
        title="Unknown",
        signals=[],
    )
    report_mod._enrich_reasons([cand], _Settings(), profiles={})
    assert '"prior_titles_sample": []' in captured["prompt"]


def test_enrich_reasons_includes_cross_source_count(monkeypatch):
    captured = {}
    monkeypatch.setattr(report_mod, "call_stage1",
                        lambda p, s, st, temperature=None: captured.setdefault("prompt", p) or "[]")
    cand = _make_candidate(
        raw_metadata={"seen_on_sources": ["a", "b", "c"]},
    )
    report_mod._enrich_reasons([cand], _Settings(), profiles={})
    assert '"cross_source_count": 3' in captured["prompt"]


def test_enrich_reasons_system_prompt_lists_anti_patterns(monkeypatch):
    captured = {}

    def fake(prompt, system, settings, temperature=None):
        captured["system"] = system
        return "[]"

    monkeypatch.setattr(report_mod, "call_stage1", fake)
    cand = _make_candidate()
    report_mod._enrich_reasons([cand], _Settings(), profiles={})
    sys_lower = captured["system"].lower()
    for banned in ["sonic", "undeniable", "journey", "vibes", "must-hear", "perfect for"]:
        assert banned in sys_lower
```

- [ ] **Step 2: Run tests — expect failure**

Expected: tests fail because the prompt/payload don't match yet (no temperature override, no prior_titles, no cross_source_count, no anti-patterns).

- [ ] **Step 3: Implement the new `_enrich_reasons`**

Replace the body of `_enrich_reasons` in `src/pipeline/report.py` with the full block from the spec (section 2). Add the missing import at the top of the file:

```python
from src.pipeline.profile import _split_artists
```

Full replacement:

```python
def _enrich_reasons(
    candidates: list[Candidate],
    settings,
    profiles: dict[str, ArtistProfile] | None = None,
) -> dict[str, str]:
    """
    Call Stage 1 LLM to generate a punchy one-sentence reason per candidate.
    Returns dict of {artist||title: reason}. Falls back to signal text on failure.

    Profiles surface concrete catalog facts (prior titles, play count) so the LLM
    has real anchors to quote rather than paraphrasing the signal text.
    """
    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}

    def _payload_for(c: Candidate) -> dict:
        signal_codes = [s.code for s in c.signals]
        best_play_count = None
        prior_titles: list[str] = []
        if "known_artist" in signal_codes:
            for part in _split_artists(c.artist):
                profile = profiles_lower.get(part.lower().strip())
                if profile:
                    if best_play_count is None or profile.play_count > best_play_count:
                        best_play_count = profile.play_count
                    for t in profile.track_titles:
                        if t and t not in prior_titles and t.lower() != c.title.lower():
                            prior_titles.append(t)
                            if len(prior_titles) >= 3:
                                break
                    if len(prior_titles) >= 3:
                        break
        return {
            "artist": c.artist,
            "title": c.title,
            "label": c.label or "",
            "source": c.source,
            "genre_tags": c.genre_tags[:5],
            "release_date": c.release_date or "",
            "chart_position": c.raw_metadata.get("chart_position"),
            "cross_source_count": len(c.raw_metadata.get("seen_on_sources", [c.source])),
            "signal_codes": signal_codes,
            "known_artist_play_count": best_play_count,
            "prior_titles_sample": prior_titles,
        }

    payload = [_payload_for(c) for c in candidates]

    system = (
        "You write concise music discovery reasons for a DJ. "
        f"{_DJ_CONTEXT} "
        "For each track, write one sentence (max 15 words) explaining why it fits this DJ's taste. "
        "Anchor on one concrete fact from the payload: a prior track by the artist, a genre tag, "
        "the chart position, or the cross-source count. Use only facts present in the payload. "
        "Avoid marketing words: sonic, undeniable, journey, vibes, must-hear, perfect for, your next favorite. "
        "Return a valid JSON array only, no preamble."
    )

    examples = [
        {
            "input": {
                "artist": "Sully", "title": "Cherry", "label": "Astrophonica", "source": "beatport",
                "genre_tags": ["breaks", "uk-bass"], "chart_position": 3, "cross_source_count": 1,
                "signal_codes": ["known_artist", "chart_position"],
                "known_artist_play_count": 4, "prior_titles_sample": ["Swandive", "Glasshouse"],
            },
            "output": {
                "artist": "Sully", "title": "Cherry",
                "reason": "Sully follow-up to Swandive, currently #3 on the Beatport breaks chart.",
            },
        },
        {
            "input": {
                "artist": "Skee Mask", "title": "Pop", "label": "Ilian Tape", "source": "juno",
                "genre_tags": ["electronica", "breaks"], "chart_position": None, "cross_source_count": 2,
                "signal_codes": ["label_match", "genre_match"],
                "known_artist_play_count": None, "prior_titles_sample": [],
            },
            "output": {
                "artist": "Skee Mask", "title": "Pop",
                "reason": "Ilian Tape release tagged electronica/breaks — picked up across two sources.",
            },
        },
    ]

    prompt = (
        "Examples:\n"
        f"{json.dumps(examples, ensure_ascii=False)}\n\n"
        f"Generate reasons for these tracks:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        'Return format: [{"artist": "...", "title": "...", "reason": "..."}]'
    )

    try:
        raw = _clean_llm_json(call_stage1(prompt, system, settings, temperature=0.3))
        enriched = json.loads(raw)
        return {
            f"{e['artist'].lower().strip()}||{e['title'].lower().strip()}": e.get("reason", "")
            for e in enriched
            if "artist" in e and "title" in e
        }
    except Exception as e:
        logger.warning(f"[report] Stage 1 reason enrichment failed: {e} — using signal fallback")
        return {}
```

- [ ] **Step 4: Run tests — expect pass**

```bash
./venv/bin/pytest tests/test_report.py -v
```
Expected: 4 tests green.

- [ ] **Step 5: Full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/report.py tests/test_report.py
git commit -m "feat(report): richer stage 1 payload, two-shot anchor, temp 0.3"
```

---

### Task 12: Stage 2 — stats injection + voice anti-patterns

**Files:**
- Modify: `tests/test_report.py` (append)
- Modify: `src/pipeline/report.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_report.py`:

```python
def test_format_weekly_stats_summarises_sections(sample_profiles):
    from src.pipeline.report import _format_weekly_stats
    sections = {
        "top_picks": [
            Candidate(artist="Sully", title="T1", link="", source="s", label="L1",
                      genre_tags=["breaks"]),
            Candidate(artist="Skee Mask", title="T2", link="", source="s", label="L1",
                      genre_tags=["breaks", "electronica"]),
        ],
        "wildcards": [
            Candidate(artist="Unknown", title="T3", link="", source="s", label="L2",
                      genre_tags=["dnb"]),
        ],
    }
    line = _format_weekly_stats(sections, sample_profiles)
    assert "3 tracks" in line
    assert "2 labels" in line
    assert "2 known artists" in line  # Sully + Skee Mask
    assert "Top genres:" in line


def test_format_weekly_stats_empty_returns_empty_string():
    from src.pipeline.report import _format_weekly_stats
    assert _format_weekly_stats({}, None) == ""
    assert _format_weekly_stats({"top_picks": []}, None) == ""


def test_format_mix_prep_stats_omits_labels_and_known_artists():
    from src.pipeline.report import _format_mix_prep_stats
    sections = {
        "top_picks": [
            Candidate(artist="A", title="T1", link="", source="s", label="L1",
                      genre_tags=["dnb"]),
            Candidate(artist="B", title="T2", link="", source="s", label="L1",
                      genre_tags=["dnb", "breaks"]),
        ],
    }
    line = _format_mix_prep_stats(sections)
    assert "2 tracks" in line
    assert "Top genres:" in line
    assert "dnb" in line
    # Labels and known artists are intentionally absent per spec.
    assert "labels" not in line
    assert "known artists" not in line


def test_format_mix_prep_stats_empty():
    from src.pipeline.report import _format_mix_prep_stats
    assert _format_mix_prep_stats({}) == ""


def test_generate_report_system_prompt_includes_anti_patterns(monkeypatch, sample_profiles):
    captured = {}

    def fake_call_stage2(prompt, system, settings):
        captured["system"] = system
        captured["prompt"] = prompt
        return "Body of report"

    def fake_call_stage1(prompt, system, settings, temperature=None):
        return "[]"

    monkeypatch.setattr(report_mod, "call_stage2", fake_call_stage2)
    monkeypatch.setattr(report_mod, "call_stage1", fake_call_stage1)

    sections = {"top_picks": [_make_candidate()]}
    report_mod.generate_report(sections, "TEST", {}, _Settings(), profiles=sample_profiles)

    sys_lower = captured["system"].lower()
    assert "sonic" in sys_lower
    assert "undeniable" in sys_lower
    assert "no filler intro" in sys_lower
    assert "no closing summary" in sys_lower
    # Stats line appears in the user prompt.
    assert "This week:" in captured["prompt"]
```

- [ ] **Step 2: Run tests — expect failure**

Expected: `_format_weekly_stats` doesn't exist; anti-patterns missing from system prompt.

- [ ] **Step 3: Implement**

In `src/pipeline/report.py`:

(a) Add the stats helper above `generate_report`:

```python
def _format_weekly_stats(
    sections: dict[str, list[Candidate]],
    profiles: dict[str, ArtistProfile] | None,
) -> str:
    """Compact stats line injected into the Stage 2 prompt as soft context."""
    all_c = [c for sec in sections.values() for c in sec]
    if not all_c:
        return ""
    total = len(all_c)
    labels = {c.label for c in all_c if c.label}
    profiles_lower = {k.lower(): v for k, v in (profiles or {}).items()}
    known_artists = set()
    for c in all_c:
        for part in _split_artists(c.artist):
            if part.lower().strip() in profiles_lower:
                known_artists.add(part.lower().strip())
    genre_counts: dict[str, int] = {}
    for c in all_c:
        for g in c.genre_tags:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    top_genres = [g for g, _ in sorted(genre_counts.items(), key=lambda kv: -kv[1])[:3]]
    return (
        f"This week: {total} tracks across {len(labels)} labels, "
        f"{len(known_artists)} known artists. "
        f"Top genres: {', '.join(top_genres) if top_genres else 'none tagged'}."
    )
```

(b) Append the anti-pattern lines to the `system` string in `generate_report` (right before the closing paren):

```python
        "Avoid marketing words: sonic, undeniable, journey, vibes, must-hear, perfect for, your next favorite. "
        "No filler intro before sections. No closing summary."
```

(c) Append identical lines to the `system` string in `generate_mix_prep_report`.

(d) Replace the prompt-construction block in `generate_report`:

```python
    stats_line = _format_weekly_stats(sections, profiles)
    prompt = (
        f"Write the weekly music discovery report for {today} (Report ID: {report_id}).\n\n"
        + (f"{stats_line}\n\n" if stats_line else "")
        + f"{sections_text}\n\n"
        "Format the full Discord report with ## section headers (with emojis), bold artist names, "
        "and track links as [Listen](<url>)."
    )
```

(e) Add a separate mix-prep stats helper that omits label/known-artist counts (the spec mandates mix-prep gets only totals + top genres, since the genre is already known by design):

```python
def _format_mix_prep_stats(sections: dict[str, list[Candidate]]) -> str:
    """Compact stats line for mix-prep — totals and top genres only.
    Labels and known-artist counts are intentionally omitted (per spec): in a
    mix-prep run the genre is fixed and the focus is the track list itself.
    """
    all_c = [c for sec in sections.values() for c in sec]
    if not all_c:
        return ""
    total = len(all_c)
    genre_counts: dict[str, int] = {}
    for c in all_c:
        for g in c.genre_tags:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    top_genres = [g for g, _ in sorted(genre_counts.items(), key=lambda kv: -kv[1])[:3]]
    return f"This set: {total} tracks. Top genres: {', '.join(top_genres) if top_genres else 'none tagged'}."
```

(f) Replace the prompt-construction block in `generate_mix_prep_report` using the mix-prep helper:

```python
    stats_line = _format_mix_prep_stats(sections)
    prompt = (
        f"Write the {genre} mix preparation report for {today} (Report ID: {report_id}).\n\n"
        + (f"{stats_line}\n\n" if stats_line else "")
        + f"{sections_text}\n\n"
        "Format the full Discord report with ## section headers (with emojis), bold artist names, "
        "and track links as [Listen](<url>)."
    )
```

- [ ] **Step 4: Run tests — expect pass**

```bash
./venv/bin/pytest tests/test_report.py -v
```
Expected: green.

- [ ] **Step 5: Full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/report.py tests/test_report.py
git commit -m "feat(report): inject weekly stats and voice anti-patterns into stage 2"
```

---

### Task 13: Bump Stage 2 temperature in `settings.yaml`

**Files:**
- Modify: `config/settings.yaml:19-24`

No new test — this is a single config value change, covered by an inline sanity check.

- [ ] **Step 1: Change the value**

In `config/settings.yaml`, in the Stage 2 block, change `temperature: 0.2` to `temperature: 0.3`. Leave all other Stage 2 fields untouched.

- [ ] **Step 2: Sanity check via config loader**

Run:
```bash
./venv/bin/python -c "
from src.config import load_settings
s = load_settings()
assert s.llm_stage2.get('temperature') == 0.3, f'got {s.llm_stage2.get(\"temperature\")}'
print('ok')
"
```
Expected: `ok`.

- [ ] **Step 3: Full suite**

```bash
./venv/bin/pytest tests/ -v
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add config/settings.yaml
git commit -m "chore(config): bump stage 2 temperature 0.2 to 0.3"
```

---

### Task 14: End-to-end dry-run validation

**Files:**
- No code changes. Inspection only.

- [ ] **Step 1: Final full test pass**

```bash
./venv/bin/pytest tests/ -v
```
Expected: every test green; reasonable total runtime (under 5s).

- [ ] **Step 2: Check-config**

```bash
./venv/bin/python -m tunefinder check-config
```
Expected: all required env vars present, no traceback.

> **Why no plain `DISCORD_BOT_TOKEN= ./venv/bin/python -m tunefinder run ...` smoke step:** `cmd_run` calls `settings.validate()` early, and `DISCORD_BOT_TOKEN` is in `_REQUIRED_ENV_VARS` (`src/config.py:12-17`). Setting the var to empty triggers `EnvironmentError` before any pipeline work runs. The CLI's existing `--dry-run` also still calls `discord.post_report` unconditionally (only prefixes with "🧪 [DRY RUN]") — `__main__.py:202-204`. So safe validation must (a) keep the real `.env` intact for `validate()` to pass, and (b) replace `make_discord_client` before `cmd_run` invokes it. The capture shim below does both.

- [ ] **Step 3: Weekly run via capture shim**

Save this to `/tmp/tf_capture.py`:

```python
"""Run cmd_run with the Discord client replaced by a file-writing capture.

Why: cmd_run calls settings.validate() which requires DISCORD_BOT_TOKEN in env,
AND the CLI's --dry-run still posts to Discord. This shim leaves env intact so
validate() passes, then swaps make_discord_client so no real network call fires.
"""
import argparse
import os
import sys

# Repo root = current working directory (the shim is launched from project root).
sys.path.insert(0, os.getcwd())

import src.output.discord as discord_mod


class _CaptureClient:
    def __init__(self, report_path: str):
        self.report_path = report_path

    def post_report(self, text: str) -> bool:
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[capture] wrote {len(text)} chars to {self.report_path}")
        return True

    def post(self, channel: str, text: str) -> bool:
        # Weekly run uses post_report, but mix-prep uses post(channel, text).
        # Capture both so the same shim works for either entrypoint.
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[capture] wrote {len(text)} chars to {self.report_path} (channel={channel})")
        return True

    def post_alert(self, message: str) -> bool:
        # Don't write alerts to the report path — log to stderr instead.
        print(f"[capture][alert] {message}", file=sys.stderr)
        return True


discord_mod.make_discord_client = lambda settings: _CaptureClient("/tmp/tunefinder-report-body.md")

# cmd_run doesn't initialise logging itself — main() does. Initialise it here so
# telemetry lands in logs/tunefinder_YYYYMMDD.log and on stdout.
from src.logger import setup_logging
setup_logging(log_dir="logs")

from tunefinder.__main__ import cmd_run

cmd_run(argparse.Namespace(command="run", dry_run=True))
```

Run it from the project root:

```bash
./venv/bin/python /tmp/tf_capture.py 2>&1 | tee /tmp/tunefinder-dry-run.log
```

Expected:
- Exit status 0, no traceback.
- Log lines show: `[ranker] Genre set: N baseline + M catalog-augmented`.
- Log lines show: `[history] N artists in 4-week recency window`.
- Log lines show: `[ranker] Scored N candidates — top score: X`.
- Log lines show: `[report] Calling Stage 2 for report generation`.
- Final line: `[capture] wrote N chars to /tmp/tunefinder-report-body.md`.

- [ ] **Step 4: Inspect captured weekly report body**

Open `/tmp/tunefinder-report-body.md` and verify:
- Output is prefixed with `🧪 **[DRY RUN — history not updated]**`.
- Each track has one short reason line (under ~20 words).
- Reasons avoid the banned marketing words: sonic, undeniable, journey, vibes, must-hear, perfect for, your next favorite.
- Label Watch section groups tracks under bold label sub-headers with italic synopsis lines.
- No bare URLs; all links use `[Listen](<url>)` form.
- No filler intro before the first section header. No closing summary after the last section.
- A `Processing Summary` footer block is present (built by `_build_footer`).

- [ ] **Step 5: Inspect ranker telemetry**

```bash
grep -E "recent_recommendation|pool_age|label_match|cross_source" /tmp/tunefinder-dry-run.log || echo "no matching signals fired this run"
```

Expected: at least one of these signals fires, or the explicit "no matching signals" message — both are acceptable depending on candidate composition.

- [ ] **Step 6: Mix-prep run via capture shim**

Save this to `/tmp/tf_capture_mix.py`:

```python
"""Run cmd_mix_prep with the Discord client replaced by a file-writing capture."""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())

import src.output.discord as discord_mod


class _CaptureClient:
    def __init__(self, report_path: str):
        self.report_path = report_path

    def post_report(self, text: str) -> bool:
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(text)
        return True

    def post(self, channel: str, text: str) -> bool:
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[capture] wrote {len(text)} chars to {self.report_path} (channel={channel})")
        return True

    def post_alert(self, message: str) -> bool:
        print(f"[capture][alert] {message}", file=sys.stderr)
        return True


discord_mod.make_discord_client = lambda settings: _CaptureClient("/tmp/tunefinder-mix-prep-body.md")

from src.logger import setup_logging
setup_logging(log_dir="logs")

from tunefinder.__main__ import cmd_mix_prep

cmd_mix_prep(argparse.Namespace(command="mix-prep", genre="dnb", dry_run=True))
```

Run it:

```bash
./venv/bin/python /tmp/tf_capture_mix.py 2>&1 | tee /tmp/tunefinder-mix-prep.log
```

Expected: exit status 0, no traceback, final `[capture] wrote N chars` line on stdout.

- [ ] **Step 7: Inspect mix-prep report body**

Open `/tmp/tunefinder-mix-prep-body.md` and verify the same anti-pattern checks as Step 4, plus:
- A `🎛️ DNB Mix Prep Report` header block at the top (built by `_build_mix_prep_header`).
- No `Label Watch` section (mix-prep has only `Top Picks` and `Deep Cuts`).
- The stats sentence, if surfaced by the LLM, references totals and top genres only — NOT label counts or known-artist counts (mix-prep uses `_format_mix_prep_stats`, not the weekly helper).

- [ ] **Step 8: Cleanup**

```bash
rm /tmp/tf_capture.py /tmp/tf_capture_mix.py /tmp/tunefinder-report-body.md /tmp/tunefinder-mix-prep-body.md /tmp/tunefinder-dry-run.log /tmp/tunefinder-mix-prep.log 2>/dev/null || true
```

- [ ] **Step 9: No commit unless a fix is needed**

If validation surfaces an issue, fix it, re-run the relevant capture step, then commit with a conventional `fix(...)` message.

---

## Self-Review

Spec coverage check:

| Spec section | Task |
|---|---|
| 1a Catalog-augmented genre set | Task 4 |
| 1b Scaled label signal | Task 5 |
| 1c Scaled cross-source signal | Task 6 |
| 1d Artist-recency penalty | Tasks 3 + 7 |
| 1e Pool age penalty | Tasks 1 + 2 + 8 |
| 2a Richer Stage 1 payload | Task 11 |
| 2b Tighter Stage 1 system prompt | Task 11 |
| 2c Two-shot anchor | Task 11 |
| 2d Per-call Stage 1 temp 0.3 | Tasks 9 + 11 |
| 3a Stage 2 stats injection | Task 12 |
| 3b Stage 2 voice anti-patterns | Task 12 |
| 3c Stage 2 temperature 0.3 | Task 13 |
| 4 Testing & guardrails | Tasks 0 + 14 + test files added per task |
| Threading `profiles` through report.py + __main__.py | Task 10 |

Type/signature consistency: `_score` evolves additively across Tasks 4 → 5 → 7 → 8 (no rename, no parameter removal). Each commit leaves a green pytest run. `_build_relevant_labels` return-type change in Task 5 is paired with the updated callers in the same commit. `_enrich_reasons` accepts `profiles` from Task 10 onward; behavior change lives in Task 11.

No placeholders. Every step contains the actual content needed.
