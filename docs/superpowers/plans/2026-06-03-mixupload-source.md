# Mixupload Source Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Mixupload as a disabled-by-default experimental TuneFinder source with genre chart + metadata support.

**Architecture:** HTML scraper using `common.get_html()` + BeautifulSoup targeting `/charts/track/style/{slug}` (top-100 with chart positions) and/or `/genres/{slug}` (recent uploads). Genre routing via `genre_map` config (Pattern A). Date normalisation from DD.MM.YY → YYYY-MM-DD.

**Tech Stack:** Python, BeautifulSoup, existing `src/fetchers/common.py` utilities.

---

## Spike Report

### Verdict: Experimental — Add as disabled-by-default source

Mixupload is publicly accessible server-rendered PHP. No Cloudflare, no captcha, no bot protection. Login modal appears on track listings but does **not** block server-rendered HTML — all metadata is in the DOM for unauthenticated requests. Downloads and streaming are login-gated, but TuneFinder does not need either.

### Access Method

- **Public HTML** — server-rendered PHP, all metadata in initial response
- No RSS feeds (all 404)
- No public REST API (`/api/` returns a 500 leaking `ctrlApi` class name but no usable endpoint)
- No embedded JSON blobs (`__NEXT_DATA__`, `window.__STATE__`, etc.)
- Scraping is the only viable method

### Metadata Availability

| Field | Available | Notes |
|---|---|---|
| artist | ✅ | In track card and track page |
| title | ✅ | In track card and track page |
| link | ✅ | `/track/{slug}-{id}` URL pattern |
| label | ✅ | `<span class="label"><a href="/labels/...">` |
| release_date | ✅ | `<span class="date">DD.MM.YY</span>` — needs normalisation |
| release_name | ❌ | No album/release grouping visible |
| genre | ✅ | Genre tags via `/genres/{slug}` links |
| chart_position | ✅ | On chart pages — inline text `2 +1` or `10 -5` |
| BPM | ✅ extra | `<span class="bpm">BPM: 130</span>` |
| key | ✅ extra | `<span class="key">KEY: Em</span>` |
| duration | ✅ extra | `M:SS` format |
| quality tier | ✅ extra | `FREE 320`, `PREMIUM FLAC`, etc. |

### Genre Coverage

| TuneFinder tag | Mixupload slug | Coverage |
|---|---|---|
| house | `house` | ✅ strong |
| dnb | `dnb` | ✅ strong |
| breaks | `breaks` | ✅ strong |
| techno | `techno` | ✅ strong |
| hip-hop | `hip-hop` | ✅ moderate |
| uk-bass | `dubstep` (approximate) | ⚠️ weak |
| ukg | — | ❌ no equivalent |
| electronica | — | ❌ no equivalent |
| downtempo | `chill` (approximate) | ⚠️ weak |
| funk-soul-jazz | — | ❌ no equivalent |

5 of 10 TuneFinder canonical genres are well-covered. The remaining 5 have no reliable Mixupload equivalent — do not attempt to map them.

### Target Genre Filtering

Supported. Chart and genre pages are per-slug (`/charts/track/style/house`). A `fetch(settings, target_genre="house")` call can skip fetching other genre slugs. Clean isolation — no cross-genre leakage.

### Risk Assessment

| Risk | Level | Notes |
|---|---|---|
| Login required | Low | HTML fully accessible without login |
| Bot protection | Low | No Cloudflare, no captcha observed |
| Scraping fragility | Medium | CSS class names (`bpm`, `key`, `date`, `label`) are stable-looking but undocumented; layout changes will break parsing |
| Stale data | Low | Chart pages appear current; date field present |
| Duplicate data | Low | `/track/{slug}-{id}` URL with numeric ID is a reliable dedup key |
| Metadata quality | Medium | BPM/key require regex extraction from inline text; date needs format normalisation |
| Search instability | Medium | `/search` endpoint returned 502 — avoid using it |

### Recommendation

**Add as disabled-by-default experimental source.** The 5 well-covered genres and chart position data make it worthwhile. Disable until a real run validates HTML parsing stability against live data. Do not attempt to cover `uk-bass`, `ukg`, `electronica`, `downtempo`, or `funk-soul-jazz` — no reliable mapping exists.

### Config Shape

```yaml
mixupload:
  enabled: false   # experimental — flip to true manually after validation
  use_charts: true  # true = /charts/track/style/{slug} (top-100 + chart position); false = /genres/{slug} (recent)
  genre_map:
    house: house
    dnb: dnb
    breaks: breaks
    techno: techno
    hip-hop: hip-hop
```

### Acceptance Criteria for Follow-up Implementation

- `fetch(settings)` returns ≥ 1 `SourceItem` per enabled genre slug without credentials (Task 3 smoke tests `house` only; verify remaining slugs manually before enabling in production)
- All returned items have non-empty `artist`, `title`, `link`, `genre_tags`
- `release_date` is ISO `YYYY-MM-DD` or `None`
- `raw_metadata["chart_position"]` is a positive integer when `use_charts: true`
- `raw_metadata["bpm"]` is an integer or `None`
- `raw_metadata["key"]` is a string like `"Em"` or absent when unparseable
- `fetch(settings, target_genre="house")` returns only items tagged `house`
- `fetch(settings, target_genre="ukg")` returns an empty list (no mapping)
- Source is registered in `__init__.py` but `enabled: false` in `settings.yaml`
- Polite sleep after each genre request attempt (including failed ones)

---

## File Map

| File | Action |
|---|---|
| `src/fetchers/mixupload.py` | Create — fetcher module |
| `src/fetchers/__init__.py` | Modify — register `mixupload` |
| `config/settings.yaml` | Modify — add disabled source config |
| `tests/test_mixupload.py` | Create — unit tests with mocked HTML |
| `tests/fixtures/mixupload_chart_house.html` | Create — fixture HTML for tests (also create `tests/fixtures/` dir) |
| `README.md` | Modify — mention Mixupload in sources table |

---

## Task 1: Unit tests (failing)

**Files:**
- Create: `tests/test_mixupload.py`
- Create: `tests/fixtures/` (new directory — does not exist yet)
- Create: `tests/fixtures/mixupload_chart_house.html`

- [ ] **Step 1: Create fixture HTML file**

  `tests/fixtures/` does not exist — create it first (`mkdir tests/fixtures`). Save one fixture file capturing the real HTML shape (minimised — only the fields we parse). This replaces live HTTP calls in tests.

  Create `tests/fixtures/mixupload_chart_house.html`:
  ```html
  <div class="tracks-list">
    <div class="track-item">
      <span class="position">1</span>
      <a class="track-name" href="/track/artist-name-track-title-1234567">Track Title</a>
      <span class="artist"><a href="/u/artist-name">Artist Name</a></span>
      <span class="label"><a href="/labels/test-records">Test Records</a></span>
      <span class="genre"><a href="/genres/house">House</a></span>
      <span class="bpm">BPM: 128</span>
      <span class="key">KEY: Am</span>
      <span class="date">01.06.26</span>
      <span class="duration">6:30</span>
    </div>
    <div class="track-item">
      <span class="position">2 +3</span>
      <a class="track-name" href="/track/dj-two-second-track-7654321">Second Track</a>
      <span class="artist"><a href="/u/dj-two">DJ Two</a></span>
      <span class="label"><a href="/labels/other-records">Other Records</a></span>
      <span class="genre"><a href="/genres/house">House</a></span>
      <span class="bpm">BPM: 124</span>
      <span class="key">KEY: Bm</span>
      <span class="date">28.05.26</span>
      <span class="duration">7:15</span>
    </div>
  </div>
  ```

  **Note:** These CSS selectors are guesses from the spike agent's description. They will be verified against a live fetch in Task 3 — adjust selectors and re-run tests at that point if needed.

- [ ] **Step 2: Write failing tests**

  `tests/test_mixupload.py`:
  ```python
  from pathlib import Path
  from unittest.mock import patch, MagicMock
  from src.fetchers import mixupload

  FIXTURE_DIR = Path(__file__).parent / "fixtures"

  def _fake_html(filename):
      return (FIXTURE_DIR / filename).read_text()

  def _make_settings(enabled=True, use_charts=True, genre_map=None):
      s = MagicMock()
      s.source_enabled.return_value = enabled
      s.get_source_config.return_value = {
          "use_charts": use_charts,
          "genre_map": genre_map or {"house": "house", "dnb": "dnb"},
      }
      return s


  def test_fetch_returns_empty_when_disabled():
      settings = _make_settings(enabled=False)
      assert mixupload.fetch(settings) == []


  def test_fetch_chart_items():
      settings = _make_settings(genre_map={"house": "house"})
      with patch("src.fetchers.mixupload.get_html") as mock_get:
          mock_get.return_value = _fake_html("mixupload_chart_house.html")
          with patch("src.fetchers.mixupload.polite_sleep"):
              items = mixupload.fetch(settings)

      assert len(items) == 2
      first = items[0]
      assert first.source == "mixupload"
      assert first.artist == "Artist Name"
      assert first.title == "Track Title"
      assert first.link == "https://mixupload.com/track/artist-name-track-title-1234567"
      assert first.label == "Test Records"
      assert first.release_date == "2026-06-01"
      assert "house" in first.genre_tags
      assert first.raw_metadata["chart_position"] == 1
      assert first.raw_metadata["bpm"] == 128
      assert first.raw_metadata["key"] == "Am"


  def test_fetch_chart_position_with_delta():
      settings = _make_settings(genre_map={"house": "house"})
      with patch("src.fetchers.mixupload.get_html") as mock_get:
          mock_get.return_value = _fake_html("mixupload_chart_house.html")
          with patch("src.fetchers.mixupload.polite_sleep"):
              items = mixupload.fetch(settings)

      second = items[1]
      assert second.raw_metadata["chart_position"] == 2  # strip the "+3" delta


  def test_fetch_target_genre_filters():
      settings = _make_settings(genre_map={"house": "house", "dnb": "dnb"})
      with patch("src.fetchers.mixupload.get_html") as mock_get:
          mock_get.return_value = _fake_html("mixupload_chart_house.html")
          with patch("src.fetchers.mixupload.polite_sleep"):
              items = mixupload.fetch(settings, target_genre="house")

      # Only house genre fetched — get_html called once (not for dnb)
      mock_get.assert_called_once()
      assert all("house" in i.genre_tags for i in items)


  def test_fetch_target_genre_no_match_returns_empty():
      settings = _make_settings(genre_map={"house": "house"})
      with patch("src.fetchers.mixupload.get_html"):
          items = mixupload.fetch(settings, target_genre="ukg")
      assert items == []


  def test_date_normalisation():
      """DD.MM.YY from site → YYYY-MM-DD ISO."""
      assert mixupload._parse_date("01.06.26") == "2026-06-01"
      assert mixupload._parse_date("28.05.26") == "2026-05-28"
      assert mixupload._parse_date("bad") is None


  def test_parse_position_strips_delta():
      assert mixupload._parse_position("1") == 1
      assert mixupload._parse_position("2 +3") == 2
      assert mixupload._parse_position("10 -5") == 10
      assert mixupload._parse_position("bad") is None


  def test_parse_key():
      assert mixupload._parse_key("KEY: Em") == "Em"
      assert mixupload._parse_key("KEY: Am") == "Am"
      assert mixupload._parse_key("bad") is None
  ```

- [ ] **Step 3: Run tests — confirm they fail**

  ```
  ./venv/bin/pytest tests/test_mixupload.py -v
  ```
  Expected: `ModuleNotFoundError` or `ImportError` for `src.fetchers.mixupload`.

---

## Task 2: Implement the fetcher

**Files:**
- Create: `src/fetchers/mixupload.py`

- [ ] **Step 1: Implement `mixupload.py`**

  ```python
  from __future__ import annotations
  import re
  from datetime import datetime
  from typing import Optional

  from src.fetchers.common import get_html, make_soup, polite_sleep
  from src.logger import get_logger
  from src.models import SourceItem

  logger = get_logger(__name__)

  _SOURCE = "mixupload"
  _BASE = "https://mixupload.com"
  _CHART_URL = _BASE + "/charts/track/style/{slug}"
  _GENRE_URL = _BASE + "/genres/{slug}"


  def fetch(settings, target_genre: str | None = None) -> list[SourceItem]:
      if not settings.source_enabled(_SOURCE):
          return []

      cfg = settings.get_source_config(_SOURCE)
      genre_map: dict[str, str] = cfg.get("genre_map", {})
      use_charts: bool = cfg.get("use_charts", True)

      if target_genre is not None and target_genre not in genre_map:
          return []

      items: list[SourceItem] = []
      pairs = [(tag, slug) for tag, slug in genre_map.items()
               if target_genre is None or tag == target_genre]

      for internal_tag, site_slug in pairs:
          url = (_CHART_URL if use_charts else _GENRE_URL).format(slug=site_slug)
          try:
              html = get_html(url)
              items.extend(_parse_tracks(html, internal_tag, use_charts))
          except Exception as e:
              logger.warning(f"[mixupload] {site_slug} fetch failed: {e}")
          polite_sleep(2.0)

      return items


  def _parse_tracks(html: str, genre_tag: str, use_charts: bool) -> list[SourceItem]:
      soup = make_soup(html)
      results = []

      for card in soup.select(".track-item"):
          artist_el = card.select_one(".artist a")
          title_el = card.select_one("a.track-name")
          label_el = card.select_one(".label a")
          bpm_el = card.select_one(".bpm")
          key_el = card.select_one(".key")
          date_el = card.select_one(".date")
          pos_el = card.select_one(".position") if use_charts else None

          if not artist_el or not title_el:
              continue

          link_path = title_el.get("href", "")
          link = _BASE + link_path if link_path.startswith("/") else link_path

          bpm_raw = bpm_el.get_text(strip=True) if bpm_el else ""
          bpm = _parse_bpm(bpm_raw)
          key = _parse_key(key_el.get_text(strip=True)) if key_el else None
          date = _parse_date(date_el.get_text(strip=True)) if date_el else None
          chart_pos = _parse_position(pos_el.get_text(strip=True)) if pos_el else None

          raw: dict = {}
          if chart_pos is not None:
              raw["chart_position"] = chart_pos
          if bpm is not None:
              raw["bpm"] = bpm
          if key is not None:
              raw["key"] = key

          results.append(SourceItem(
              source=_SOURCE,
              artist=artist_el.get_text(strip=True),
              title=title_el.get_text(strip=True),
              link=link,
              label=label_el.get_text(strip=True) if label_el else None,
              release_date=date,
              release_name=None,
              genre_tags=[genre_tag],
              raw_metadata=raw,
          ))

      return results


  def _parse_date(raw: str) -> Optional[str]:
      """Convert DD.MM.YY to YYYY-MM-DD. Returns None on parse failure."""
      try:
          return datetime.strptime(raw.strip(), "%d.%m.%y").strftime("%Y-%m-%d")
      except ValueError:
          return None


  def _parse_position(raw: str) -> Optional[int]:
      """Extract leading integer from strings like '2 +3' or '10 -5'."""
      m = re.match(r"(\d+)", raw.strip())
      return int(m.group(1)) if m else None


  def _parse_bpm(raw: str) -> Optional[int]:
      """Extract integer from 'BPM: 128'."""
      m = re.search(r"(\d+)", raw)
      return int(m.group(1)) if m else None


  def _parse_key(raw: str) -> Optional[str]:
      """Extract key string from 'KEY: Em' → 'Em'."""
      m = re.search(r"KEY:\s*(.+)", raw.strip(), re.IGNORECASE)
      return m.group(1).strip() if m else None
  ```

- [ ] **Step 2: Run tests**

  ```
  ./venv/bin/pytest tests/test_mixupload.py -v
  ```
  Expected: all tests pass. If CSS selectors don't match fixture HTML, adjust `_parse_tracks` selectors.

---

## Task 3: Validate HTML selectors against live site

**Important:** CSS selectors used in `_parse_tracks` are derived from the spike agent's description. They must be verified before shipping.

- [ ] **Step 1: Fetch a live chart page**

  ```bash
  ./venv/bin/python -c "
  from src.fetchers.common import get_html, make_soup
  html = get_html('https://mixupload.com/charts/track/style/house')
  soup = make_soup(html)
  cards = soup.select('.track-item')
  print(f'Found {len(cards)} track cards')
  if cards:
      print(cards[0].prettify()[:2000])
  "
  ```

- [ ] **Step 2: Adjust selectors**

  If `.track-item`, `.artist a`, `a.track-name`, `.label a`, `.bpm`, `.date`, `.position` don't match the live HTML, update them in `_parse_tracks`. Update fixture HTML to match the real structure. Re-run tests after.

- [ ] **Step 3: Smoke fetch against live HTML**

  No registration needed — call the module directly with a mock settings:

  ```bash
  ./venv/bin/python -c "
  from unittest.mock import MagicMock
  from src.fetchers import mixupload

  settings = MagicMock()
  settings.source_enabled.return_value = True
  settings.get_source_config.return_value = {
      'use_charts': True,
      'genre_map': {'house': 'house'},
  }
  items = mixupload.fetch(settings)
  print(f'Fetched {len(items)} items')
  for i in items[:5]:
      print(i.artist, '-', i.title, '|', i.release_date, '|', i.raw_metadata)
  "
  ```

  Expected: ≥1 item, `release_date` in YYYY-MM-DD, `chart_position` as int in `raw_metadata`.

---

## Task 4: Register source and add config

**Files:**
- Modify: `src/fetchers/__init__.py`
- Modify: `config/settings.yaml`

- [ ] **Step 1: Register in `__init__.py`**

  `src/fetchers/__init__.py` uses a `_FETCHERS` list of `(name, fetch_fn)` tuples (line 19) and a grouped import on line 11. Make two changes:

  Line 11 — add `mixupload` to the import:
  ```python
  from src.fetchers import bandcamp, beatport, bleep, boomkat, juno, mixupload, ra, traxsource
  ```

  `_FETCHERS` list — append after the last existing entry:
  ```python
  ("mixupload", mixupload.fetch),
  ```

- [ ] **Step 2: Add disabled config to `settings.yaml`**

  Add under the `sources:` key (after existing sources):
  ```yaml
  mixupload:
    enabled: false   # experimental — validate HTML selectors before enabling
    use_charts: true  # true = top-100 chart with chart_position; false = recent genre feed
    genre_map:
      house: house
      dnb: dnb
      breaks: breaks
      techno: techno
      hip-hop: hip-hop
  ```

- [ ] **Step 3: Validate config**

  ```bash
  ./venv/bin/python -m tunefinder check-config
  ```
  Expected: passes with no errors (requires all normally-needed env vars to be present; a missing Discord token or LLM key will still fail as usual — that is not a regression).

- [ ] **Step 4: Run full test suite**

  ```bash
  ./venv/bin/pytest tests/ -v
  ```
  Expected: all existing tests still pass.

---

## Task 5: README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add Mixupload row to sources table**

  The sources table in `README.md` has three columns: `Source | Method | Status`. Add:
  ```
  | Mixupload | HTML scrape (chart pages) | disabled (experimental) |
  ```

- [ ] **Step 2: Add Mixupload column to genre coverage table**

  The genre coverage table at `## Genre coverage` has columns `Genre | Beatport | Traxsource | Bandcamp`. Add a `Mixupload` column. Only the 5 covered genres get slugs; the rest get `—`:

  ```
  | Genre | Beatport | Traxsource | Bandcamp | Mixupload |
  |---|---|---|---|---|
  | `house` | ... | ... | ... | house |
  | `dnb` | ... | ... | ... | dnb |
  | `breaks` | ... | ... | ... | breaks |
  | `uk-bass` | ... | ... | ... | — |
  | `ukg` | ... | ... | ... | — |
  | `electronica` | ... | ... | ... | — |
  | `downtempo` | ... | ... | ... | — |
  | `techno` | ... | ... | ... | techno |
  | `funk-soul-jazz` | ... | ... | ... | — |
  | `hip-hop` | ... | ... | ... | hip-hop |
  ```

  Keep the existing cell values unchanged — only append the Mixupload column.

- [ ] **Step 3: Update chart_position scoring note**

  Line: `| \`chart_position\` | +0–1.5 | Linear decay from #1 (Beatport; Traxsource when enabled) |`

  Update to include Mixupload:
  ```
  | `chart_position` | +0–1.5 | Linear decay from #1 (Beatport; Mixupload and Traxsource when enabled) |
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add src/fetchers/mixupload.py src/fetchers/__init__.py \
          config/settings.yaml tests/test_mixupload.py \
          tests/fixtures/mixupload_chart_house.html README.md
  git commit -m "feat(sources): add mixupload as disabled experimental source"
  ```

---

## Verification

1. `./venv/bin/python -m tunefinder check-config` — passes (env vars must be present)
2. `./venv/bin/pytest tests/ -v` — all pass including new mixupload tests
3. Task 3 Step 3 smoke fetch passes — items returned, `release_date` is ISO, `chart_position` is int (run after Task 2, before Task 4)
4. `mixupload.enabled` is `false` in `config/settings.yaml` before final commit
