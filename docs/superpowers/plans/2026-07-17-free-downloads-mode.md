# Free Downloads Mode Implementation Plan (TuneFinder)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `tunefinder free-downloads <genre>` run mode that reports the best 30 free SoundCloud downloads (native + gated) for a genre, reusing the mix-prep engine, with the web API extended so tunefinder-web can trigger and render it.

**Architecture:** `MixPrepOptions` gains `free_only`; `run_mix_prep` restricts fetching to `pipeline.free_download_sources`, filters candidates to track-level free eligibility, and produces a single Free Downloads section. The SoundCloud fetcher learns gate detection, richer parsing (bpm/key/artist/reposts), and flex-aware server-side BPM ranges. Spec: `docs/superpowers/specs/2026-07-17-free-downloads-mode-design.md` (approved 2026-07-17).

**Tech Stack:** Python 3.13, requests, FastAPI/pydantic, pytest (mock external IO — never live Discord/SoundCloud in tests).

**Scope note:** This plan covers the TuneFinder repo only (backend + CLI + web API). The tunefinder-web SPA work (spec §7) is a separate plan in that repo, written after this lands — its type-regen step needs this backend.

## Global Constraints

- Existing behaviour of weekly and mix-prep runs must be byte-identical when the new config keys are absent and no new metadata is present (snapshot tests enforce this; the only deliberate snapshot changes are the new gate markers on tracks carrying the new metadata).
- All new config keys default in code: `pipeline.free_downloads_mode_count: 30`, `sources.soundcloud.include_gated_free: true`, `scoring.soundcloud_popularity_reposts: 25`.
- Report id convention: `{make_report_id()}-free-dl-{genre}` (e.g. `2026-W29-free-dl-dnb`). Artifact/RunOutcome kind: `"free-downloads"`. Feedback `history` literal stays `weekly | mix-prep`.
- Server-side `bpm[]` params are sent ONLY on free-only runs; weekly/mix-prep never send them.
- No new runtime dependencies. Conventional commits, no Co-Authored-By trailers.
- Run tests with `./venv/bin/pytest`; validate config with `./venv/bin/python -m tunefinder check-config`.

---

### Task 1: `expand_bpm_ranges` in harmonic.py

**Files:**
- Modify: `src/pipeline/harmonic.py` (append after `bpm_matches`, ~line 135)
- Test: `tests/test_harmonic.py` (append)

**Interfaces:**
- Produces: `expand_bpm_ranges(bpm_range: tuple[float, float], flex: bool = True) -> list[tuple[float, float]]` — `[(lo, hi)]` when `flex=False`; `[(lo, hi), (lo/2, hi/2), (lo*2, hi*2)]` when `flex=True`. Consumed by Task 8 (`run_mix_prep`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_harmonic.py`):

```python
from src.pipeline.harmonic import expand_bpm_ranges


def test_expand_bpm_ranges_no_flex_single_range():
    assert expand_bpm_ranges((170.0, 180.0), flex=False) == [(170.0, 180.0)]


def test_expand_bpm_ranges_flex_adds_half_and_double():
    assert expand_bpm_ranges((170.0, 180.0), flex=True) == [
        (170.0, 180.0), (85.0, 90.0), (340.0, 360.0),
    ]


def test_expand_bpm_ranges_flex_default_on():
    assert len(expand_bpm_ranges((120.0, 130.0))) == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_harmonic.py -k expand_bpm -v`
Expected: FAIL — `ImportError: cannot import name 'expand_bpm_ranges'`

- [ ] **Step 3: Implement** (append to `src/pipeline/harmonic.py`):

```python
def expand_bpm_ranges(bpm_range: tuple[float, float], flex: bool = True) -> list[tuple[float, float]]:
    """Server-side search ranges matching bpm_matches() semantics: with flex on,
    a 170-180 request must also return 85-90 (half-time) and 340-360 (double)
    tagged tracks — a single exact range would strip tracks the client-side
    filter is contractually meant to accept."""
    lo, hi = bpm_range
    if not flex:
        return [(lo, hi)]
    return [(lo, hi), (lo / 2, hi / 2), (lo * 2, hi * 2)]
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_harmonic.py -v`
Expected: all PASS (existing harmonic tests untouched).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/harmonic.py tests/test_harmonic.py
git commit -m "feat: add expand_bpm_ranges for flex-aware server-side BPM search"
```

---

### Task 2: SoundCloud parse extras (artist, bpm, key, reposts, release stash)

**Files:**
- Modify: `src/fetchers/soundcloud.py:150-180` (`_parse_track`)
- Test: `tests/test_soundcloud.py` (append; extend `_track` helper)

**Interfaces:**
- Produces (raw_metadata keys on SoundCloud `SourceItem`s): `bpm`, `key` (from `key_signature`), `reposts_count`, `release_year`, `release_month`, `release_day`. Artist field prefers `metadata_artist`. Consumed by Tasks 5 (ranker), 7 (report), and the existing harmonic filters (`candidate_bpm` reads `raw_metadata["bpm"]`; `candidate_camelot` reads `raw_metadata["key"]`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soundcloud.py`; also add the new API fields to the `_track` helper — add these keys to the dict it returns: `"metadata_artist": None, "bpm": None, "key_signature": None, "reposts_count": 7, "release_year": None, "release_month": None, "release_day": None`):

```python
def test_parse_extracts_bpm_key_reposts_and_release_fields(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track()
    track.update({"bpm": 174.0, "key_signature": "Am", "reposts_count": 33,
                  "release_year": 2005, "release_month": 6, "release_day": 1})
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)

    md = items[0].raw_metadata
    assert md["bpm"] == 174.0
    assert md["key"] == "Am"
    assert md["reposts_count"] == 33
    assert (md["release_year"], md["release_month"], md["release_day"]) == (2005, 6, 1)
    # release_* are display-only stash — the pipeline release date stays upload-derived
    assert items[0].release_date == "2026-07-10"


def test_parse_prefers_metadata_artist_over_username(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track()
    track["metadata_artist"] = "Real Artist"
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)
    assert items[0].artist == "Real Artist"


def test_parse_blank_metadata_artist_falls_back_to_username(tmp_path):
    settings = _make_settings(tmp_path)
    track = _track()
    track["metadata_artist"] = "   "
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)
    assert items[0].artist == "Test DJ"
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_soundcloud.py -k "metadata_artist or bpm_key_reposts" -v`
Expected: FAIL — `KeyError: 'key'` / artist assertion mismatch.

- [ ] **Step 3: Implement** — in `_parse_track`, replace the artist line and extend raw_metadata:

```python
    artist = (track.get("metadata_artist") or "").strip() \
        or ((track.get("user") or {}).get("username") or "").strip()
```

and add to the `raw_metadata` dict (after `"duration_ms"`):

```python
            "bpm": track.get("bpm"),
            "key": track.get("key_signature"),
            "reposts_count": track.get("reposts_count"),
            # Display-only stash — deliberately NOT the pipeline release_date:
            # a 2005 bootleg uploaded yesterday must survive the 28-day window.
            "release_year": track.get("release_year"),
            "release_month": track.get("release_month"),
            "release_day": track.get("release_day"),
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_soundcloud.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/soundcloud.py tests/test_soundcloud.py
git commit -m "feat: extract artist/bpm/key/reposts/release metadata from SoundCloud tracks"
```

---

### Task 3: Gate detection, `free_download` stamp, `acquisition_url`

**Files:**
- Modify: `src/fetchers/soundcloud.py` (`_parse_track`, fetch keep-condition ~line 230; new `_is_free_gate` helper)
- Test: `tests/test_soundcloud.py` (append)

**Interfaces:**
- Produces (raw_metadata): `free_gate: bool`, `free_download: bool` (True iff native-downloadable or free gate), `acquisition_url: str | None` (gate `purchase_url` when gated; permalink when native; None otherwise). Config: `sources.soundcloud.include_gated_free` (default True) read from the source config dict. Consumed by Tasks 5–8.
- `_parse_track(track, tag, free_gate=False)` — new keyword.

- [ ] **Step 1: Write the failing tests** (append):

```python
def test_gated_free_dl_kept_and_flagged(tmp_path):
    settings = _make_settings(tmp_path)  # downloadable_only=True
    gated = _track(downloadable=False)   # purchase_title "Free Download", hypeddit URL
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([gated])
        items = soundcloud.fetch(settings)

    assert len(items) == 1
    md = items[0].raw_metadata
    assert md["free_gate"] is True
    assert md["free_download"] is True
    assert md["acquisition_url"] == "https://hypeddit.com/dl/xyz"


def test_gate_domain_without_free_title_kept(tmp_path):
    settings = _make_settings(tmp_path)
    gated = _track(downloadable=False)
    gated["purchase_title"] = "Download"          # no "free", but hypeddit host
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([gated])
        items = soundcloud.fetch(settings)
    assert len(items) == 1 and items[0].raw_metadata["free_gate"] is True


def test_paid_purchase_link_still_dropped(tmp_path):
    settings = _make_settings(tmp_path)
    paid = _track(downloadable=False)
    paid["purchase_title"] = "Buy on Bandcamp"
    paid["purchase_url"] = "https://artist.bandcamp.com/track/x"
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([paid])
        items = soundcloud.fetch(settings)
    assert items == []


def test_include_gated_free_false_restores_native_only(tmp_path):
    settings = _make_settings(tmp_path)
    settings.get_source_config.return_value["include_gated_free"] = False
    gated = _track(downloadable=False)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([gated])
        items = soundcloud.fetch(settings)
    assert items == []


def test_native_download_stamped_with_permalink_acquisition(tmp_path):
    settings = _make_settings(tmp_path)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([_track(downloadable=True)])
        items = soundcloud.fetch(settings)
    md = items[0].raw_metadata
    assert md["free_gate"] is False
    assert md["free_download"] is True
    assert md["acquisition_url"] == items[0].link


def test_downloadable_only_false_non_free_not_stamped(tmp_path):
    settings = _make_settings(tmp_path, downloadable_only=False)
    plain = _track(downloadable=False)
    plain["purchase_title"] = None
    plain["purchase_url"] = None
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([plain])
        items = soundcloud.fetch(settings)
    assert len(items) == 1
    assert items[0].raw_metadata["free_download"] is False


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "hypeddit.com/dl/xyz",       # scheme-less — urlparse yields no netloc
    "   ",
])
def test_unsafe_or_invalid_purchase_url_never_gates(tmp_path, bad_url):
    settings = _make_settings(tmp_path)
    track = _track(downloadable=False)   # purchase_title "Free Download"
    track["purchase_url"] = bad_url
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([track])
        items = soundcloud.fetch(settings)
    assert items == []                    # not a gate → dropped by downloadable_only


def test_gate_url_query_string_preserved(tmp_path):
    settings = _make_settings(tmp_path)
    gated = _track(downloadable=False)
    gated["purchase_url"] = "https://hypeddit.com/dl/xyz?sig=abc123&fan=1"
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        mock_get.return_value = _page([gated])
        items = soundcloud.fetch(settings)
    assert items[0].raw_metadata["acquisition_url"] == "https://hypeddit.com/dl/xyz?sig=abc123&fan=1"
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_soundcloud.py -k "gated or gate_domain or paid_purchase or stamped" -v`
Expected: FAIL — gated track filtered out / `KeyError: 'free_gate'`.

- [ ] **Step 3: Implement.** Add near `_DATE_RE`:

```python
# Hypeddit/ToneDen-style "Free DL" gates: downloadable=false but the purchase
# link is a free-download exchange, not a store. Title match is primary; the
# domain allowlist catches gates whose title omits "free".
_GATE_DOMAINS = {"hypeddit.com", "toneden.io", "gate.fm"}


def _is_free_gate(track: dict) -> bool:
    if track.get("downloadable") is True:
        return False
    url = (track.get("purchase_url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    # http(s)-with-host only — purchase_url is uploader-controlled and lands in
    # hrefs downstream (audition page, SPA cards); a javascript:/data: URL must
    # never qualify as a "gate". html.escape does not neutralise URL schemes.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if "free" in (track.get("purchase_title") or "").lower():
        return True
    host = parsed.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return host in _GATE_DOMAINS
```

`_parse_track` gains a keyword and the stamp fields:

```python
def _parse_track(track: dict, tag: str, free_gate: bool = False) -> SourceItem | None:
```

and inside `raw_metadata` (after the Task 2 additions):

```python
            "free_gate": free_gate,
            "free_download": bool(track.get("downloadable") is True or free_gate),
            # Gate URLs are preserved verbatim (already scheme/host-validated by
            # _is_free_gate) — gates use required/signed query params, so no
            # query stripping here, unlike the permalink's utm cleanup.
            "acquisition_url": (
                (track.get("purchase_url") or "").strip() if free_gate
                else link if track.get("downloadable") is True
                else None
            ),
```

In `fetch()`, read the flag with the other config (`include_gated = cfg.get("include_gated_free", True)`) and replace the keep-condition:

```python
                for track in (data.get("collection") or []):
                    gate = include_gated and _is_free_gate(track)
                    if downloadable_only and track.get("downloadable") is not True and not gate:
                        continue
```

and pass it through: `item = _parse_track(track, tag, free_gate=gate)`.

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_soundcloud.py -v`
Expected: all PASS (existing tests keep passing — the default `_track()` is downloadable, so `gate` stays False for them).

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/soundcloud.py tests/test_soundcloud.py
git commit -m "feat: detect gated free downloads and stamp track-level free eligibility"
```

---

### Task 4: Server-side BPM ranges in the SoundCloud fetcher

**Files:**
- Modify: `src/fetchers/soundcloud.py` (`fetch` signature + search loop, `_build_search_url`)
- Test: `tests/test_soundcloud.py` (append)

**Interfaces:**
- Produces: `soundcloud.fetch(settings, target_genre=None, bpm_ranges=None)` where `bpm_ranges: list[tuple[float, float]] | None`. One search per range per target, `bpm[from]`/`bpm[to]` in the query, results deduped by SoundCloud track id across ranges. `_build_search_url(target, created_from, limit, bpm_range=None)`. Consumed by Task 8 via `fetch_all_sources` (Task 7 plumbs it).

- [ ] **Step 1: Write the failing tests** (append):

```python
def test_build_search_url_includes_bpm_params():
    url = soundcloud._build_search_url({"tf_tag": "dnb", "genres": "dnb"}, "2026-06-19", 50,
                                       bpm_range=(170.0, 180.0))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["bpm[from]"] == ["170"] and q["bpm[to]"] == ["180"]


def test_build_search_url_without_bpm_unchanged():
    url = soundcloud._build_search_url({"tf_tag": "dnb", "genres": "dnb"}, "2026-06-19", 50)
    assert "bpm" not in url


def test_fetch_multi_range_searches_and_dedupes(tmp_path):
    settings = _make_settings(tmp_path)
    shared, half_only = _track(track_id=1), _track(track_id=2)
    with _patch_token(), patch("src.fetchers.soundcloud._get_json") as mock_get:
        # range 1 returns [shared]; range 2 returns [shared, half_only]; range 3 empty
        mock_get.side_effect = [_page([shared]), _page([shared, half_only]), _page([])]
        items = soundcloud.fetch(settings, bpm_ranges=[(170, 180), (85, 90), (340, 360)])

    assert mock_get.call_count == 3
    assert [i.raw_metadata["soundcloud_id"] for i in items] == [1, 2]
    called_urls = [c.args[0] for c in mock_get.call_args_list]
    assert "bpm%5Bfrom%5D=170" in called_urls[0]
    assert "bpm%5Bfrom%5D=85" in called_urls[1]
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_soundcloud.py -k "bpm_params or multi_range or without_bpm" -v`
Expected: FAIL — `TypeError: _build_search_url() got an unexpected keyword argument 'bpm_range'`.

- [ ] **Step 3: Implement.** `_build_search_url` gains the param (BPM values formatted with `:g` so `170.0` → `170`):

```python
def _build_search_url(target: dict, created_from: str, limit: int,
                      bpm_range: tuple[float, float] | None = None) -> str:
    params: dict = {}
    for key in ("q", "genres", "tags"):
        val = (target.get(key) or "").strip()
        if val:
            params[key] = val
    if bpm_range is not None:
        params["bpm[from]"] = f"{bpm_range[0]:g}"
        params["bpm[to]"] = f"{bpm_range[1]:g}"
    params["created_at[from]"] = f"{created_from} 00:00:00"
    params["limit"] = limit
    params["linked_partitioning"] = "true"
    return f"{_API_BASE}/tracks?{urllib.parse.urlencode(params)}"
```

`fetch` gains `bpm_ranges: list[tuple[float, float]] | None = None` and the per-target loop wraps the existing pagination in a range loop with id-dedup. Replace the body of the `for i, target in enumerate(targets):` loop's search section (keep the existing try/except and page-cap structure per range):

```python
        tag_items: list[SourceItem] = []
        seen_ids: set = set()
        target_ok = False
        search_ranges: list[tuple[float, float] | None] = list(bpm_ranges) if bpm_ranges else [None]
        for range_no, bpm_range in enumerate(search_ranges):
            if range_no:
                polite_sleep(1.0)
            # try/except sits INSIDE the range loop: one flaky range must not
            # discard tag_items already collected from earlier ranges
            # (graceful degradation — matches the single-range behaviour).
            try:
                url: str | None = _build_search_url(target, created_from, limit, bpm_range=bpm_range)
                page = 0
                while url and page < _MAX_PAGES:
                    logger.info(f"[soundcloud] {tag}: page {page + 1} — {url}")
                    data = _get_json(url, session)
                    page += 1
                    for track in (data.get("collection") or []):
                        track_id = track.get("id")
                        if track_id is not None and track_id in seen_ids:
                            continue
                        gate = include_gated and _is_free_gate(track)
                        if downloadable_only and track.get("downloadable") is not True and not gate:
                            continue
                        duration = track.get("duration")
                        if max_duration_ms and duration and duration > max_duration_ms:
                            continue
                        item = _parse_track(track, tag, free_gate=gate)
                        if item is None:
                            continue
                        if item.release_date is not None and item.release_date < created_from:
                            continue
                        if track_id is not None:
                            seen_ids.add(track_id)
                        tag_items.append(item)
                    url = data.get("next_href")
                target_ok = True
            except Exception as e:
                logger.warning(f"[soundcloud] {tag}: range {range_no + 1} fetch failed: {e}")
                continue
        if not target_ok:
            # Every range failed — the target must NOT count as completed, or
            # the "all N targets failed" RuntimeError fail-safe below never
            # fires (a total outage would read as "0 tracks, no error").
            continue
```

(`completed += 1` sits directly after this block, outside the snippet — the `if not target_ok: continue` guard must land immediately before it. A target with at least one successful range still counts as completed and keeps that range's `tag_items`; `test_all_targets_failed_raises` and `test_one_failed_target_continues` in tests/test_soundcloud.py both stay green. The two in-code comments that existed on the keep-conditions — duration cap and client-side lookback — stay where those lines moved.)

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_soundcloud.py -v`
Expected: all PASS — existing single-search tests exercise the `[None]` range path unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/soundcloud.py tests/test_soundcloud.py
git commit -m "feat: flex-aware server-side BPM range search for SoundCloud"
```

---

### Task 5: Config keys + ScoringWeights + settings.yaml

**Files:**
- Modify: `src/config.py` (after `pipeline_free_downloads_min_score`, ~line 140), `src/pipeline/ranker.py:63-64` area (ScoringWeights), `config/settings.yaml`
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `settings.pipeline_free_downloads_mode_count -> int` (default 30); `ScoringWeights.soundcloud_popularity_reposts: int = 25`. Consumed by Tasks 6 and 8.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`, matching its existing accessor-default test style):

```python
def test_free_downloads_mode_count_default(minimal_settings):
    assert minimal_settings.pipeline_free_downloads_mode_count == 30


def test_scoring_weights_reposts_default():
    from src.pipeline.ranker import ScoringWeights
    assert ScoringWeights().soundcloud_popularity_reposts == 25
```

(If `tests/test_config.py` has no `minimal_settings` fixture, mirror how its existing tests construct a Settings from a minimal dict — reuse their exact pattern.)

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_config.py -k "mode_count or reposts_default" -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement.** `src/config.py`, after `pipeline_free_downloads_min_score`:

```python
    @property
    def pipeline_free_downloads_mode_count(self) -> int:
        return self._data.get("pipeline", {}).get("free_downloads_mode_count", 30)
```

`src/pipeline/ranker.py` ScoringWeights, directly under `soundcloud_popularity_downloads`:

```python
    soundcloud_popularity_reposts: int = 25    # minimum reposts_count that also fires the signal
```

`config/settings.yaml` — under `pipeline:` next to the other free-download keys:

```yaml
  free_downloads_mode_count: 30       # slots for the dedicated free-downloads mode report
```

under `sources: soundcloud:` next to `downloadable_only`:

```yaml
    include_gated_free: true    # keep Hypeddit/ToneDen-style "Free DL" gated tracks (downloadable=false)
```

under `scoring:` next to `soundcloud_popularity_downloads`:

```yaml
  soundcloud_popularity_reposts: 25    # Minimum reposts_count that also earns the popularity signal
```

- [ ] **Step 4: Run + config check**

Run: `./venv/bin/pytest tests/test_config.py -v && ./venv/bin/python -m tunefinder check-config`
Expected: tests PASS; check-config validates.

- [ ] **Step 5: Commit**

```bash
git add src/config.py src/pipeline/ranker.py config/settings.yaml tests/test_config.py
git commit -m "feat: config keys for free-downloads mode, gated DLs, repost popularity"
```

---

### Task 6: Ranker — repost-aware popularity + lane-count override; reasons variant

**Files:**
- Modify: `src/pipeline/ranker.py:515-523` (signal), `:804-873` (`_assign_sections_mix_prep`), `:876-925` (`rank_candidates_mix_prep`); `src/pipeline/reasons.py:77-80` area and `:244-251`
- Test: `tests/test_ranker.py`, `tests/test_reasons.py` (append)

**Interfaces:**
- Produces: `rank_candidates_mix_prep(..., free_downloads_count: int | None = None)` → `_assign_sections_mix_prep(..., free_downloads_count=None)` overriding `lane_n`. Signal fires on downloads OR reposts; explanation `"{n} reposts on SoundCloud."` when reposts alone trigger. Consumed by Task 8.

- [ ] **Step 1: Write the failing tests.** `tests/test_ranker.py` (mirror its existing candidate/settings helpers — reuse the module's `_candidate`/settings-mock pattern verbatim):

```python
def test_soundcloud_reposts_fire_popularity_signal():
    c = _candidate(source="soundcloud", raw_metadata={"reposts_count": 30, "download_count": 3})
    _score_single(c)  # use the module's existing minimal-scoring harness
    assert any(s.code == "source_popularity" and "30 reposts on SoundCloud." == s.explanation
               for s in c.signals)


def test_soundcloud_downloads_take_precedence_in_explanation():
    c = _candidate(source="soundcloud", raw_metadata={"reposts_count": 99, "download_count": 60})
    _score_single(c)
    assert any(s.explanation == "60 downloads on SoundCloud." for s in c.signals)


def test_mix_prep_free_downloads_count_override():
    # Unique artists are load-bearing: _assign_sections_mix_prep caps
    # MAX_PER_ARTIST = 2 per lane, so 40 same-artist candidates would only
    # ever yield 2. Candidates must also be scored.
    ranked = [_scored_candidate(0.5, artist=f"DJ {i}", title=f"B{i}", source="soundcloud")
              for i in range(40)]
    sections = _assign_sections_mix_prep(ranked, _lane_settings(), free_downloads_count=30)
    assert len(sections["free_downloads"]) == 30
```

(`_lane_settings()` = the settings object the module's existing free-downloads-lane tests use (its `_LaneSettings`-style helper) with `pipeline_free_download_sources = ["soundcloud"]` and `pipeline_mix_prep_free_downloads_count = 10`. Mirror the module's real lane tests — reuse its actual `_scored_candidate`/settings helpers; the assertions above are the contract.)

`tests/test_reasons.py`:

```python
def test_reason_reposts_only_popularity_line():
    c = _c(source="soundcloud", raw_metadata={"reposts_count": 30})
    c.signals = [RecommendationSignal(code="source_popularity",
                                      explanation="30 reposts on SoundCloud.")]
    reason = compose_reason(c, {}, today=TODAY)
    assert "30 reposts on SoundCloud" in reason


def test_reason_reposts_trigger_beats_low_download_count():
    # download_count=3 is positive (so dl_count gets set at reasons.py:77-80),
    # but the signal fired on reposts — the reason must say reposts, not
    # "grabbed 3 times".
    c = _c(source="soundcloud", raw_metadata={"reposts_count": 30, "download_count": 3})
    c.signals = [RecommendationSignal(code="source_popularity",
                                      explanation="30 reposts on SoundCloud.")]
    reason = compose_reason(c, {}, today=TODAY)
    assert "30 reposts on SoundCloud" in reason
    assert "downloads" not in reason and "grabbed" not in reason
```

(Reuse `test_reasons.py`'s existing candidate builder and TODAY constant. NOTE: the module's `_c` helper rebuilds its `signals` argument from code *strings* with empty explanations — and the new reasons branch keys off the explanation text — so set `c.signals` directly after construction as shown, rather than passing signal objects through `_c`.)

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_ranker.py tests/test_reasons.py -k "reposts or count_override" -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** Ranker signal block (replace lines 515–523):

```python
    # --- SoundCloud popularity signal (discovery axis; free-DL lane) ---
    reposts_count = c.raw_metadata.get("reposts_count")
    downloads_fire = (download_count is not None and isinstance(download_count, int)
                      and download_count >= weights.soundcloud_popularity_downloads)
    reposts_fire = (isinstance(reposts_count, int)
                    and reposts_count >= weights.soundcloud_popularity_reposts)
    if c.source == "soundcloud" and (downloads_fire or reposts_fire):
        score += weights.w_soundcloud_popularity
        discovery += weights.w_soundcloud_popularity
        explanation = (f"{download_count} downloads on SoundCloud." if downloads_fire
                       else f"{reposts_count} reposts on SoundCloud.")
        c.signals.append(RecommendationSignal(code="source_popularity", explanation=explanation))
```

`_assign_sections_mix_prep` — signature and lane_n:

```python
def _assign_sections_mix_prep(
    ranked: list[Candidate],
    settings,
    demoted_keys: set[str] | None = None,
    free_downloads_count: int | None = None,
) -> dict[str, list[Candidate]]:
```

```python
    lane_n = (free_downloads_count if free_downloads_count is not None
              else settings.pipeline_mix_prep_free_downloads_count)
```

`rank_candidates_mix_prep` — add `free_downloads_count: int | None = None` to the signature (after `skip_penalty_artists`) and thread it:

```python
    return _assign_sections_mix_prep(ranked, settings, demoted_keys=demoted_keys,
                                     free_downloads_count=free_downloads_count), label_artist_names
```

`src/pipeline/reasons.py`: next to the `dl_count` extraction (~line 77), add:

```python
    reposts_count: Optional[int] = None
    raw_reposts = c.raw_metadata.get("reposts_count")
    if isinstance(raw_reposts, int):
        reposts_count = raw_reposts
```

and **before** the existing `source_popularity`/`dl_count` branch (line 244–251), add a trigger-derived branch — the trigger must come from the signal itself, because `dl_count` is set for ANY positive download count (reasons.py:77-80) and a low-but-nonzero count would otherwise win the template:

```python
    if "source_popularity" in signal_codes and reposts_count is not None:
        sp_expl = next((s.explanation for s in c.signals if s.code == "source_popularity"), "")
        if "reposts" in sp_expl:
            # {r} replaced before _fill so _fill only sees placeholders it knows.
            return _fill(f"DJs are reposting this — {reposts_count} reposts on {{source_disp}}.")
```

(The existing downloads branch below it is unchanged and now only renders when the signal actually fired on downloads.)

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_ranker.py tests/test_reasons.py tests/test_report.py -v`
Expected: all PASS — existing snapshots unaffected (no existing fixture carries `reposts_count`).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/ranker.py src/pipeline/reasons.py tests/test_ranker.py tests/test_reasons.py
git commit -m "feat: repost-aware SoundCloud popularity and free-downloads lane count override"
```

---

### Task 7: `fetch_all_sources` plumbing — `only_sources` + `bpm_ranges`

**Files:**
- Modify: `src/fetchers/__init__.py:34-67`; every fetcher signature: `src/fetchers/{bandcamp,beatport,bleep,boomkat,mixupload,ra,traxsource,volumo}.py` (`def fetch(settings, target_genre=None)` → add `bpm_ranges=None`; soundcloud already accepts it from Task 4)
- Test: `tests/test_sources_archive.py` or new `tests/test_fetch_all_sources.py` (create if no aggregator tests exist)

**Interfaces:**
- Produces: `fetch_all_sources(settings, target_genre=None, only_sources=None, bpm_ranges=None)`. `only_sources` (a list/set of source names) skips non-listed fetchers entirely — they appear in neither items nor health. `bpm_ranges` forwarded to every fetcher (all ignore it except soundcloud). Consumed by Task 8.

- [ ] **Step 1: Write the failing test** (`tests/test_fetch_all_sources.py`):

```python
from unittest.mock import MagicMock, patch

from src.fetchers import fetch_all_sources


def _settings_all_enabled():
    s = MagicMock()
    s.source_enabled = MagicMock(return_value=True)
    return s


def test_only_sources_restricts_fetchers():
    # _FETCHERS binds function objects at import time, so patching
    # src.fetchers.soundcloud.fetch would NOT intercept — patch the registry.
    s = _settings_all_enabled()
    sc, bp = MagicMock(return_value=[]), MagicMock(return_value=[])
    with patch("src.fetchers._FETCHERS", [("soundcloud", sc), ("beatport", bp)]):
        items, health = fetch_all_sources(s, only_sources=["soundcloud"])
    sc.assert_called_once()
    bp.assert_not_called()
    assert "beatport" not in health and "soundcloud" in health


def test_bpm_ranges_forwarded_to_fetchers():
    s = _settings_all_enabled()
    sc = MagicMock(return_value=[])
    with patch("src.fetchers._FETCHERS", [("soundcloud", sc)]):
        fetch_all_sources(s, only_sources=["soundcloud"], bpm_ranges=[(170.0, 180.0)])
    assert sc.call_args.kwargs["bpm_ranges"] == [(170.0, 180.0)]
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_fetch_all_sources.py -v`
Expected: FAIL — `TypeError: fetch_all_sources() got an unexpected keyword argument 'only_sources'`.

- [ ] **Step 3: Implement.** `fetch_all_sources`:

```python
def fetch_all_sources(settings, target_genre: str | None = None,
                      only_sources: list[str] | None = None,
                      bpm_ranges: list[tuple[float, float]] | None = None) -> tuple[list[SourceItem], dict[str, dict]]:
```

docstring gains: `only_sources restricts the run to the named fetchers (free-downloads mode); bpm_ranges is forwarded to fetchers — those without server-side BPM search ignore it.` — and in the loop, after the enabled check:

```python
        if only_sources is not None and name not in only_sources:
            continue
```

and the call becomes `items = fetch_fn(settings, target_genre=target_genre, bpm_ranges=bpm_ranges)`.

Each other fetcher's signature gains the ignored kwarg, e.g. `def fetch(settings, target_genre=None, bpm_ranges=None):` (8 one-line edits; no body changes).

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/ -v -x -q`
Expected: full suite PASS (fetcher tests call `fetch(settings)` positionally — unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/fetchers/
git add tests/test_fetch_all_sources.py
git commit -m "feat: only_sources and bpm_ranges plumbing in fetch_all_sources"
```

---

### Task 8: `run_mix_prep` free_only branch

**Files:**
- Modify: `src/services/runs.py` (`MixPrepOptions`, `run_mix_prep`), `src/pipeline/report.py` (`_build_mix_prep_header`, `generate_mix_prep_report`)
- Test: `tests/test_services_runs.py`, `tests/test_report.py` (append)

**Interfaces:**
- Consumes: Tasks 1, 5, 6, 7 interfaces.
- Produces: `MixPrepOptions.free_only: bool = False`. Free-only runs: report id `{make_report_id()}-free-dl-{genre}`, `RunOutcome.kind == "free-downloads"`, artifact kind `"free-downloads"`, header `🆓 {Display Genre} Free Downloads Report`, candidates filtered to `raw_metadata["free_download"] is True` (fresh + pool), mix-prep history append unchanged. `generate_mix_prep_report(..., free_only: bool = False)`.

- [ ] **Step 1: Write the failing tests.** `tests/test_report.py`:

```python
def test_free_downloads_mode_header_and_single_section():
    sections = {"top_picks": [], "deep_cuts": [],
                "free_downloads": [_c(title="Boot", source="soundcloud")]}
    text = generate_mix_prep_report(sections, "2026-W29-free-dl-dnb", {}, "dnb", object(),
                                    today=TODAY, free_only=True)
    assert text.startswith("🆓 Dnb Free Downloads Report")
    assert "Mix Prep Report" not in text
    assert "## 🔺 Top Picks" not in text and "## 🎧 Deep Cuts" not in text
    assert "## 🆓 Free Downloads" in text
```

`tests/test_services_runs.py` (reuse `_settings`, `_known_track`, `_patched` — plus a soundcloud item helper):

```python
def _free_item(title="Boot VIP", free=True):
    md = {"soundcloud_id": 1, "download_count": 60}
    if free:
        md.update({"free_download": True, "free_gate": False,
                   "acquisition_url": "https://soundcloud.com/x/y"})
    return SourceItem(
        source="soundcloud", artist="Someone", title=title, link="https://soundcloud.com/x/y",
        label=None, release_date=None, genre_tags=["dnb"], raw_metadata=md,
    )


def _seed_pool(data_dir):
    """A free SoundCloud pool record and a paid Beatport one — the free_only
    eligibility filter must let only the first into the report."""
    from src.models import PoolRecord
    from src.pipeline.pool import save_pool
    save_pool([
        PoolRecord(artist="PoolFree", title="Pool Boot", link="https://soundcloud.com/p/f",
                   source="soundcloud", label=None, release_date=None, release_name=None,
                   genre_tags=["dnb"],
                   raw_metadata={"free_download": True, "free_gate": False,
                                 "acquisition_url": "https://soundcloud.com/p/f"},
                   added_at="2026-07-01T00:00:00+00:00", last_score=1.0),
        PoolRecord(artist="PoolPaid", title="Pool Paid", link="https://example.com/p",
                   source="beatport", label=None, release_date=None, release_name=None,
                   genre_tags=["dnb"], raw_metadata={"beatport_id": 7},
                   added_at="2026-07-01T00:00:00+00:00", last_score=1.0),
    ], data_dir)


def test_run_free_only_restricts_fetch_and_filters_eligibility(tmp_path):
    settings = _settings(str(tmp_path))
    settings.pipeline_free_download_sources = ["soundcloud"]
    settings.pipeline_free_downloads_mode_count = 30
    _seed_pool(str(tmp_path))
    options = MixPrepOptions(genre="dnb", dry_run=True, free_only=True)
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources",
               return_value=([_free_item(), _free_item(title="Paid Leak", free=False)],
                             {"soundcloud": {"count": 2, "error": None}})) as mock_fetch, \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        outcome = run_mix_prep(settings, options)

    assert mock_fetch.call_args.kwargs["only_sources"] == ["soundcloud"]
    assert outcome.kind == "free-downloads"
    assert "-free-dl-dnb" in outcome.report_id
    assert outcome.artifact["kind"] == "free-downloads"
    titles = [t["title"] for s in outcome.artifact["sections"] for t in s["tracks"]]
    # fresh: free kept, paid dropped; pool: free injected, paid filtered
    assert "Boot VIP" in titles and "Paid Leak" not in titles
    assert "Pool Boot" in titles and "Pool Paid" not in titles


def test_run_free_only_forwards_expanded_bpm_ranges(tmp_path):
    settings = _settings(str(tmp_path))
    settings.pipeline_free_download_sources = ["soundcloud"]
    settings.pipeline_free_downloads_mode_count = 30
    options = MixPrepOptions(genre="dnb", bpm_range=(170.0, 180.0), dry_run=True, free_only=True)
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources",
               return_value=([_free_item()], {"soundcloud": {"count": 1, "error": None}})) as mock_fetch, \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        run_mix_prep(settings, options)
    assert mock_fetch.call_args.kwargs["bpm_ranges"] == [(170.0, 180.0), (85.0, 90.0), (340.0, 360.0)]


def test_run_mix_prep_regular_sends_no_only_sources_or_bpm_ranges(tmp_path):
    settings = _settings(str(tmp_path))
    options = MixPrepOptions(genre="breaks", bpm_range=(130.0, 140.0), dry_run=True)
    with patch("src.fetchers.catalog.fetch_all_tracks", return_value=[_known_track()]), \
         patch("src.fetchers.catalog.fetch_all_mixes", return_value=[]), \
         patch("src.fetchers.fetch_all_sources",
               return_value=([_source_item()], {"beatport": {"count": 1, "error": None}})) as mock_fetch, \
         patch("src.output.discord.make_discord_client", return_value=MagicMock()):
        run_mix_prep(settings, options)
    assert mock_fetch.call_args.kwargs.get("only_sources") is None
    assert mock_fetch.call_args.kwargs.get("bpm_ranges") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_services_runs.py tests/test_report.py -k "free_only or free_downloads_mode" -v`
Expected: FAIL — `TypeError: MixPrepOptions.__init__() got an unexpected keyword argument 'free_only'`.

- [ ] **Step 3: Implement.**

`MixPrepOptions` gains `free_only: bool = False`.

`report.py` — `_build_mix_prep_header` gains `free_only: bool = False`; the first line becomes:

```python
    title = (f"🆓 {display_genre} Free Downloads Report" if free_only
             else f"🎛️ {display_genre} Mix Prep Report")
    lines = [
        title,
```

`generate_mix_prep_report` gains `free_only: bool = False` (after `filters_desc`) and passes it: `header = _build_mix_prep_header(report_id, today_str, genre, filters_desc=filters_desc, free_only=free_only)`. (Top Picks/Deep Cuts already render conditionally — empty sections vanish with no further change.)

`run_mix_prep` changes, in order:

1. After `genre = options.genre`: `free_only = options.free_only`.
2. Report id: `report_id = f"{make_report_id()}-free-dl-{genre}" if free_only else f"{make_report_id()}-mix-prep-{genre}"`.
3. Fetch (replace the existing call):

```python
        only_sources = settings.pipeline_free_download_sources if free_only else None
        fetch_bpm_ranges = None
        if free_only and bpm_range is not None:
            from src.pipeline.harmonic import expand_bpm_ranges
            fetch_bpm_ranges = expand_bpm_ranges(bpm_range, bpm_flex)
        source_items, fetcher_health = fetch_all_sources(
            settings, target_genre=genre, only_sources=only_sources, bpm_ranges=fetch_bpm_ranges,
        )
```

4. Eligibility filter — immediately after `candidates = filter_known(candidates, known_keys, remix_aware)`:

```python
        if free_only:
            # Track-level free eligibility (spec §3.1) — the report's title
            # promises "free"; a downloadable_only:false config or a mixed
            # future lane source must not leak paid tracks here.
            candidates = [c for c in candidates if c.raw_metadata.get("free_download") is True]
```

5. Pool injection — after the existing `pool_injected = [...]` filter:

```python
        if free_only:
            pool_injected = [c for c in pool_injected if c.raw_metadata.get("free_download") is True]
```

6. Ranking call gains `free_downloads_count=settings.pipeline_free_downloads_mode_count if free_only else None`.
7. Report/artifact:

```python
        report_text = generate_mix_prep_report(
            sections, report_id, stats, genre, settings, profiles=profiles, label_artists=label_artists,
            aliases=aliases, filters_desc=filters_desc, free_only=free_only,
        )
        artifact = build_report_artifact(
            sections, report_id, "free-downloads" if free_only else "mix-prep", stats,
            profiles=profiles, label_artists=label_artists, aliases=aliases,
            genre=genre, filters=filters_payload, dry_run=dry_run,
        )
```

8. Both `RunOutcome(...)` constructions in the function: `kind="free-downloads" if free_only else "mix-prep"`.
9. Log prefix stays `[mix-prep]` (it is the same engine); the start log line gains the mode: `logger.info(f"[mix-prep] Starting {'free-downloads' if free_only else 'mix-prep'} run — genre: {genre} — {report_id}" + ...)`.

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_services_runs.py tests/test_report.py tests/test_report_artifact.py -v`
Expected: all PASS, including untouched mix-prep tests (default `free_only=False` path byte-identical).

- [ ] **Step 5: Commit**

```bash
git add src/services/runs.py src/pipeline/report.py tests/test_services_runs.py tests/test_report.py
git commit -m "feat: free-only mode branch in the mix-prep engine"
```

---

### Task 9: Gate rendering (Discord + audition) and artifact payload fields

**Files:**
- Modify: `src/pipeline/report.py:232-252` (`_track_line`), `src/pipeline/audition.py:158-161`, `src/pipeline/report_artifact.py:55-88` (`_track_payload`)
- Test: `tests/test_report.py`, `tests/test_report_artifact.py`, `tests/test_audition.py` (append)

**Interfaces:**
- Consumes: raw_metadata `free_gate` / `free_download` / `acquisition_url` (Task 3).
- Produces: Discord track line suffix — gated: `· 🔗 [Get](<acquisition_url>)`; native free: `· ⬇️`. Audition: a `Get ↗` anchor when `acquisition_url` present and different from `link`. Artifact payload keys: `free_gate: bool`, `acquisition_url: str | None`. Consumed by Task 10 (schema) and the SPA plan.

- [ ] **Step 1: Write the failing tests.** `tests/test_report.py`:

```python
def test_track_line_gated_free_dl_gets_get_link():
    c = _c(title="Gated Boot", source="soundcloud",
           raw_metadata={"free_gate": True, "free_download": True,
                         "acquisition_url": "https://hypeddit.com/dl/xyz"})
    text = generate_report({"free_downloads": [c]}, "TEST", {}, object(), today=TODAY)
    assert "· 🔗 [Get](<https://hypeddit.com/dl/xyz>)" in text


def test_track_line_native_free_dl_marker():
    c = _c(title="Native Boot", source="soundcloud",
           raw_metadata={"free_gate": False, "free_download": True,
                         "acquisition_url": "https://soundcloud.com/x/y"})
    text = generate_report({"free_downloads": [c]}, "TEST", {}, object(), today=TODAY)
    assert "· ⬇️" in text and "[Get]" not in text


def test_track_line_without_free_metadata_unchanged():
    c = _c(title="Store Track")
    text = generate_report({"top_picks": [c]}, "TEST", {}, object(), today=TODAY)
    assert "⬇️" not in text and "🔗" not in text
```

`tests/test_report_artifact.py` (mirror its payload-assertion style):

```python
def test_track_payload_carries_free_gate_and_acquisition_url():
    c = _c(source="soundcloud",
           raw_metadata={"free_gate": True, "free_download": True,
                         "acquisition_url": "https://hypeddit.com/dl/xyz"})
    payload = _payload_for(c)  # the module's existing single-candidate payload helper
    assert payload["free_gate"] is True
    assert payload["acquisition_url"] == "https://hypeddit.com/dl/xyz"


def test_track_payload_defaults_without_free_metadata():
    payload = _payload_for(_c())
    assert payload["free_gate"] is False
    assert payload["acquisition_url"] is None
```

`tests/test_audition.py`:

```python
def test_audition_gated_track_gets_get_anchor():
    c = _c(source="soundcloud",
           raw_metadata={"free_gate": True, "free_download": True,
                         "acquisition_url": "https://hypeddit.com/dl/xyz"})
    html_out = _render_single(c)  # the module's existing single-track render helper
    assert 'href="https://hypeddit.com/dl/xyz"' in html_out and "Get ↗" in html_out
```

(Reuse each test module's existing builders; the assertions are the contract. If a helper doesn't exist, build the minimal sections dict inline exactly as neighbouring tests do.)

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_report.py tests/test_report_artifact.py tests/test_audition.py -k "gated or native or without_free or free_gate" -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

`report.py` `_track_line` — before the return, add:

```python
    free_str = ""
    if c.raw_metadata.get("free_gate"):
        acq = c.raw_metadata.get("acquisition_url")
        free_str = f" · 🔗 [Get](<{acq}>)" if acq else " · 🔗"
    elif c.raw_metadata.get("free_download"):
        free_str = " · ⬇️"
    return f"{n}. **{c.artist} — {c.title}**{label_str}{source_str}{link_str}{harmonic_str}{free_str}"
```

`report_artifact.py` `_track_payload` — add after `"pool_added_at"`:

```python
        "free_gate": bool(c.raw_metadata.get("free_gate")),
        "acquisition_url": c.raw_metadata.get("acquisition_url"),
```

`audition.py` — after the `link_html` line:

```python
    acq = c.raw_metadata.get("acquisition_url")
    if acq and acq != c.link:
        acq_safe = html.escape(str(acq))
        link_html += f' <a href="{acq_safe}" target="_blank" style="color:#6fa;font-size:12px;">Get ↗</a>'
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_report.py tests/test_report_artifact.py tests/test_audition.py -v`
Expected: all PASS — `test_weekly_snapshot` and existing free-lane tests unchanged (their candidates carry none of the new metadata, so every suffix is empty).

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/report.py src/pipeline/report_artifact.py src/pipeline/audition.py tests/test_report.py tests/test_report_artifact.py tests/test_audition.py
git commit -m "feat: render gate/native free-download markers and acquisition URLs"
```

---

### Task 10: Web layer — schemas, report kind, run dispatch, feedback resolution

**Files:**
- Modify: `src/web/schemas.py:40-64,73-99,196-231`, `src/web/app.py:99`, `src/web/reportdata.py:22-26`, `src/web/jobs.py:117-150`
- Test: `tests/test_web_api.py` (append)

**Interfaces:**
- Consumes: artifact kind + payload fields (Tasks 8–9), `MixPrepOptions.free_only` (Task 8).
- Produces: `"free-downloads"` accepted/emitted by: `ReportSummary.kind`, `ReportDetail.kind`, `RunRequest.mode`, `JobSummary.mode` (and thus `JobDetail`), reports-list `kind` query; `report_kind("2026-W29-free-dl-dnb") == ("free-downloads", "dnb")`; `build_options({"mode": "free-downloads", ...})` → `("free-downloads", MixPrepOptions(..., free_only=True))`. `ReportTrack` gains `free_gate: bool = False`, `acquisition_url: Optional[str] = None`. Feedback on free-dl report ids resolves to history `"mix-prep"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_web_api.py`, following its existing client/fixture conventions):

```python
def test_report_kind_free_dl_derivation():
    from src.web.reportdata import report_kind
    assert report_kind("2026-W29-free-dl-dnb") == ("free-downloads", "dnb")
    assert report_kind("2026-W29-mix-prep-dnb") == ("mix-prep", "dnb")
    assert report_kind("2026-W29") == ("weekly", None)


def test_build_options_free_downloads_mode():
    from src.web.jobs import build_options
    mode, options = build_options({"mode": "free-downloads", "genre": "dnb",
                                   "bpm_min": 170, "bpm_max": 180})
    assert mode == "free-downloads"
    assert options.free_only is True
    assert options.genre == "dnb" and options.bpm_range == (170.0, 180.0)


def test_build_options_free_downloads_requires_valid_genre():
    from src.web.jobs import build_options, JobValidationError
    import pytest
    with pytest.raises(JobValidationError):
        build_options({"mode": "free-downloads", "genre": "polka"})


def test_reports_list_accepts_free_downloads_kind(client):
    # the client fixture enables bearer auth — every request needs the
    # module's AUTH headers or it 401s before reaching the handler
    resp = client.get("/api/reports?kind=free-downloads", headers=AUTH)
    assert resp.status_code == 200


def test_feedback_on_free_dl_report_resolves_mix_prep_history(client, seeded_free_dl_report):
    # seeded_free_dl_report: append a RecommendationRecord with
    # report_id "2026-W29-free-dl-dnb", track_no 1 via
    # src.pipeline.history.append_mix_prep_records into the test data_dir
    # (mirror how existing feedback tests seed mix-prep history).
    resp = client.post("/api/feedback", headers=AUTH,
                       json={"outcome": "liked",
                             "report_id": "2026-W29-free-dl-dnb",
                             "track_no": 1})
    assert resp.status_code == 200
    assert resp.json()["history"] == "mix-prep"
```

(`client` and history-seeding fixtures: reuse `test_web_api.py`'s existing ones — it already builds an app over a tmp data_dir and seeds histories for its feedback tests; copy that exact pattern for `seeded_free_dl_report`.)

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_web_api.py -k "free_dl or free_downloads" -v`
Expected: FAIL — derivation returns `("weekly", None)`; `build_options` raises unknown mode; kind query 422s.

- [ ] **Step 3: Implement.**

`schemas.py`: change all four literals to `Literal["weekly", "mix-prep", "free-downloads"]` (`ReportSummary.kind`, `ReportDetail.kind`, `RunRequest.mode`, `JobSummary.mode`). `FeedbackResponse.history` stays two-valued. `ReportTrack` gains, after `pool_added_at`:

```python
    free_gate: bool = False
    acquisition_url: Optional[str] = None
```

`app.py:99`: pattern becomes `pattern="^(weekly|mix-prep|free-downloads)$"`.

`reportdata.py` `report_kind`, before the mix-prep branch:

```python
    if "-free-dl-" in report_id:
        return "free-downloads", report_id.split("-free-dl-", 1)[1]
```

(No other `reportdata.py` change: feedback resolution's existing `weekly if kind == "weekly" else mix_prep` branch now lands free-downloads ids in mix-prep history — the new test pins this.)

`jobs.py` `build_options`: change the mix-prep branch to serve both modes:

```python
    if mode in ("mix-prep", "free-downloads"):
```

and the return at its end:

```python
        return mode, MixPrepOptions(
            genre=genre, bpm_range=bpm_range, key_camelot=key_camelot,
            bpm_flex=bool(request.get("bpm_flex", True)), dry_run=dry_run,
            free_only=(mode == "free-downloads"),
        )
```

Update the trailing error message: `valid: weekly, mix-prep, free-downloads`. The runner dispatch (`if job.mode == "weekly": ... else: run_mix_prep(...)`) is already correct for the new mode — leave it, the build_options test covers routing.

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/pytest tests/test_web_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/web/ tests/test_web_api.py
git commit -m "feat: free-downloads mode across web schemas, report kinds, and run dispatch"
```

---

### Task 11: CLI command, README, full validation

**Files:**
- Modify: `tunefinder/__main__.py` (new `cmd_free_downloads`, shared filter parsing, subparser, dispatch), `README.md`
- Test: `tests/test_cli_mix_prep.py` or new `tests/test_cli_free_downloads.py`

**Interfaces:**
- Consumes: `run_mix_prep` + `MixPrepOptions(free_only=True)` (Task 8), `MIX_PREP_GENRES`.
- Produces: `tunefinder free-downloads <genre> [--bpm MIN-MAX] [--key KEY] [--no-bpm-flex] [--dry-run]`.

- [ ] **Step 1: Write the failing test** (`tests/test_cli_free_downloads.py`, mirroring `tests/test_cli_mix_prep.py`'s patching pattern):

```python
from unittest.mock import MagicMock, patch

import pytest

import tunefinder.__main__ as cli
from src.services.runs import RunOutcome


def _outcome():
    return RunOutcome(kind="free-downloads", report_id="2026-W29-free-dl-dnb",
                      dry_run=True, recommended_count=3, duration_seconds=1)


def test_cli_free_downloads_invokes_free_only_options(capsys):
    settings = MagicMock()
    with patch.object(cli, "load_settings", return_value=settings), \
         patch("src.services.runs.run_mix_prep", return_value=_outcome()) as mock_run, \
         patch("sys.argv", ["tunefinder", "free-downloads", "dnb", "--bpm", "170-180", "--dry-run"]):
        cli.main()

    options = mock_run.call_args.args[1]
    assert options.free_only is True
    assert options.genre == "dnb"
    assert options.bpm_range == (170.0, 180.0)
    assert options.dry_run is True
    assert "free-dl" in capsys.readouterr().out


def test_cli_free_downloads_rejects_unknown_genre():
    with patch("sys.argv", ["tunefinder", "free-downloads", "polka"]):
        with pytest.raises(SystemExit):
            cli.main()
```

(Match `test_cli_mix_prep.py`'s actual patch targets — if it patches `tunefinder.__main__.load_settings` or the runs module differently, copy that style exactly.)

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/pytest tests/test_cli_free_downloads.py -v`
Expected: FAIL — argparse `invalid choice: 'free-downloads'`.

- [ ] **Step 3: Implement.** In `tunefinder/__main__.py`:

Extract the BPM/key parsing shared with `cmd_mix_prep` (lines 136–158) into a module-level helper and use it from both commands:

```python
def _parse_filter_args(args) -> tuple[tuple[float, float] | None, str | None, bool]:
    """Fail-fast --bpm/--key/--no-bpm-flex parsing shared by mix-prep and
    free-downloads. Exits with a clean message on invalid values."""
    from src.pipeline.harmonic import to_camelot

    bpm_range = None
    bpm_arg = getattr(args, "bpm", None)
    if bpm_arg:
        try:
            bpm_range = _parse_bpm_range(bpm_arg)
        except ValueError as exc:
            print(f"Error: {exc}")
            raise SystemExit(1)

    key_camelot = None
    key_arg = getattr(args, "key", None)
    if key_arg:
        key_camelot = to_camelot(key_arg)
        if key_camelot is None:
            print(
                f"Error: could not parse --key {key_arg!r} — use Camelot notation "
                "(e.g. 8A) or a musical key (e.g. Am, C major)"
            )
            raise SystemExit(1)

    return bpm_range, key_camelot, not getattr(args, "no_bpm_flex", False)
```

(`cmd_mix_prep` shrinks to call it; behaviour identical.) New command:

```python
def cmd_free_downloads(args):
    from src.pipeline.storage import RunLockHeldError
    from src.services.runs import MixPrepOptions, run_mix_prep

    bpm_range, key_camelot, bpm_flex = _parse_filter_args(args)
    dry_run = getattr(args, "dry_run", False)

    settings = load_settings()
    settings.validate()

    options = MixPrepOptions(
        genre=args.genre, bpm_range=bpm_range, key_camelot=key_camelot,
        bpm_flex=bpm_flex, dry_run=dry_run, free_only=True,
    )
    try:
        outcome = run_mix_prep(settings, options)
    except RunLockHeldError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    if outcome.no_candidates:
        return
    print(f"Free-downloads run complete — {outcome.report_id} — {outcome.recommended_count} tracks in {outcome.duration_seconds}s"
          + (" (DRY RUN — no writes)" if dry_run else ""))
```

Subparser (after the mix-prep block in `main()`), reusing the exact `--bpm`/`--key`/`--no-bpm-flex`/`--dry-run` argument definitions from the mix-prep parser (same help strings):

```python
    free_dl_parser = subparsers.add_parser(
        "free-downloads",
        help="Genre-focused report of the best free downloads (SoundCloud native + gated)",
    )
    free_dl_parser.add_argument("genre", choices=list(MIX_PREP_GENRES), help="Genre to focus on")
```

…then the four shared optional arguments copied verbatim from the mix-prep parser. Dispatch: `elif args.command == "free-downloads": cmd_free_downloads(args)`.

`README.md`: add a `### free-downloads` command section beside mix-prep documenting usage, the 30-slot default (`pipeline.free_downloads_mode_count`), gated-DL behaviour (`sources.soundcloud.include_gated_free`), shared mix-prep history semantics, the mix-prep Discord channel, and the strict server-side `--bpm` note. Also mention `scoring.soundcloud_popularity_reposts` wherever the README lists scoring keys.

- [ ] **Step 4: Full validation**

Run: `./venv/bin/pytest tests/ -q && ./venv/bin/python -m tunefinder check-config`
Expected: full suite PASS; config validates.
OPTIONAL live smoke check — only if SoundCloud creds are present in `.env`; skip entirely (and say so in the PR) if they are absent or the run is unattended-restricted. `--dry-run` is mandatory both times (no Discord post, no history writes): `./venv/bin/python -m tunefinder free-downloads dnb --dry-run` — inspect the logged report preview; separately re-run with `--bpm 170-180 --dry-run` and check the logs for whether the `bpm[]`-filtered searches return plausibly-filtered results (the spec's live-verification gate: if the API ignores `bpm[]`, note it in the README section — the client-side path still applies). This check is advisory and must never block the PR.

- [ ] **Step 5: Commit**

```bash
git add tunefinder/__main__.py README.md tests/test_cli_free_downloads.py
git commit -m "feat: tunefinder free-downloads CLI command"
```

---

## Self-Review Notes

- Spec §1 → Task 11; §2 → Tasks 6–8; §3.1 → Tasks 3, 9; §3.2 → Tasks 1, 4, 8; §3.3–3.4 → Task 2; §4 → Tasks 5–6; §5 → Tasks 5, 11; §6 → Task 10; §8 → every task's test steps + Task 11 step 4. §7 (SPA) is deliberately out — separate plan in tunefinder-web.
- Helper names referenced from test modules (`_c`, `_payload_for`, `_render_single`, `_settings_with_lane`, `minimal_settings`, `seeded_free_dl_report`) are contracts to satisfy with each module's real builders — implementers must mirror the neighbouring tests' actual helpers rather than invent parallel scaffolding; the assertions are the requirement.
- Type consistency: `bpm_ranges: list[tuple[float, float]]` end-to-end (Tasks 1→4→7→8); `free_downloads_count: int | None` (6→8); artifact/RunOutcome kind string `"free-downloads"` (8→10); raw_metadata keys `free_download`/`free_gate`/`acquisition_url` (3→8→9→10).
