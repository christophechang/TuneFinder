# Volumo Source Spike — 2026-06-04

## 1. Recommended Verdict

**Fit for purpose** as a curated new-release source for house, DnB, techno, breaks, UKG, and electronica. API is live, returns rich metadata, and works without authentication. Primary limitation: no audio previews (paid store). Suitable as a disabled-by-default experimental source pending a follow-up implementation task.

---

## 2. Access Method Found

**Official REST API** — `https://volumo.com/api/v1/`

The API is semi-documented (discovered by inspecting the Next.js `__NEXT_DATA__` JSON and `_app` JS bundle). It is not publicly advertised on the site but is stable: the app's own SSR layer uses it directly. Tokens are created via account settings.

---

## 3. Authentication

| Property | Detail |
|---|---|
| Token type | Personal Access Token (UUID format) |
| Where to store | `VOLUMO_API_KEY` env var; **never** in `settings.yaml` |
| Required for browsing | **No** — unauthenticated requests return full catalog data |
| Bad/missing token behaviour | API silently ignores bad token; still returns results |
| Scopes / expiry | Not exposed in API responses |
| Rate limits | No `X-RateLimit-*` headers observed; 6 rapid requests all 200 OK at ~120ms each |

**Unauthenticated fallback:** fully possible. The `listened`, `purchased`, `playlisted`, `downloaded` fields on tracks will all be null/false/0, which is fine for TuneFinder.

---

## 4. Metadata Availability

### Per Track (returned inline within album response)

| Field | Available | Notes |
|---|---|---|
| `artist` | ✅ | Array of `{id, name, type}` — join with `, ` |
| `title` | ✅ | Track title only (`title` field) |
| `version` | ✅ | Remix/version string (`version` field, nullable) |
| `link` | ✅ (constructed) | `https://volumo.com/track/{id}-{slug}` |
| `label` | ✅ | `recordlabel.name`, with stable `recordlabel.id` |
| `release_date` | ✅ | `release_start_at` ISO-8601 (e.g. `2026-06-03T21:00:00Z`) |
| `release_name` | ✅ | `album.title` |
| `genre_id` | ✅ | Numeric, stable, mappable to internal tags |
| `chart_position` | ❌ | Not available in new-release feed |
| `bpm` | ✅ | Integer |
| `keysign` | ✅ | e.g. `"C major"`, `"A♭ minor"` |
| `duration` | ✅ | Milliseconds float |
| `isrc` | ✅ | |
| `catalog_number` | ✅ | `album.catalog_number` |
| `formats` | ✅ | Available formats + filesizes (`mp3`, `wav`, `aiff`, `flac`) |
| `preview URL` | ❌ | Paid store — no free previews |
| `stable track ID` | ✅ | Integer `id` (e.g. `6335989`) |
| `stable album ID` | ✅ | Integer `id` (e.g. `1684117`) |
| `stable artist ID` | ✅ | Integer `id` |
| `stable label ID` | ✅ | Integer `id` |

### Date Field Reliability

`release_start_at` is valid for all properly ingested releases. A subset of older catalog entries have corrupted dates (`0009-01-29`) — these are filtered out automatically by the `release_start_from` filter. Always use `release_start_from` in the API filter to avoid surfacing corrupted entries.

**Recommendation:** use `release_start_at` from the album and track objects. Fall back to `album.first_live` (ISO-8601 timestamp of when the album went live on Volumo) if `release_start_at` appears invalid. **Note:** `first_live` is inferred from API inspection; verify the exact field name in the live response during implementation.

---

## 5. Genre Coverage

Volumo has 35 genres with stable integer IDs and URL slugs.

### Mapping to TuneFinder canonical tags

| TuneFinder tag | Volumo genre(s) | ID(s) | Coverage |
|---|---|---|---|
| `house` | House | 12 | Strong |
| `house` | Deep House | 4 | Strong |
| `house` | Tech House | 21 | Strong |
| `house` | Soulful House | 20 | Good |
| `house` | Funky / Jackin' House | 10 | Good |
| `house` | Progressive House | 19 | Good |
| `house` | Melodic House / Techno | 15 | Good |
| `house` | Afro House | 1 | Good |
| `dnb` | Drum and Bass | 6 | Strong |
| `techno` | Techno (Raw, Deep, Dub) | 22 | Strong |
| `techno` | Techno (Peak Time) | 23 | Strong |
| `breaks` | Breaks / Breakbeat | 3 | Good |
| `ukg` | UK Garage / 2-Step | 25 | Good |
| `electronica` | Electronica | 8 | Good |
| `downtempo` | Organic House / Downtempo | 18 | Partial |
| `uk-bass` | Bass House / Future House | 2 | Partial (not pure UK bass) |
| `funk-soul-jazz` | Nu-Disco / Soul / Funk | 17 | Partial |
| `hip-hop` | Soul / R&B / Hip-Hop | 29 | Partial |

**Well-covered:** house (all sub-genres), dnb, techno, breaks, ukg, electronica.
**Weak/partial:** downtempo, uk-bass, funk-soul-jazz, hip-hop (each maps loosely).

---

## 6. Target Genre Filtering

**Fully supported.** The `filter` parameter accepts `{"genres":[id1,id2,...]}` to fetch albums matching those genre IDs. The fetcher maps internal tags to genre ID lists at config time.

Multiple genre IDs can be passed in a single request, or individual calls can be made per genre. A single call per internal TuneFinder tag is the cleanest approach.

**Reliability:** genre IDs are stable integers returned by `GET /api/v1/genres`. Genres don't appear to be deleted or renumbered. Genre slugs are also stable.

**Edge cases:** 
- `house` maps to many Volumo sub-genres; configuring which sub-genres to include is left to the operator.
- Releases can belong to multiple genres (e.g. `genres: [6, 8]`). This rarely matters because the `filter` call will surface them under whichever genre the operator configures.

---

## 7. API Practicality

| Property | Detail |
|---|---|
| Base URL | `https://volumo.com/api/v1/` |
| Primary endpoint | `GET /api/v1/albums` |
| Filter format | JSON string in `filter` query param |
| Pagination | `limit` + `offset` query params; no total count in list response |
| Total count | `GET /api/v1/albums_stats?filter={same JSON}` → `{"albums_total": N, "tracks_total": N}` |
| Sort (recommended) | `sort=purchase` — sorts by purchase-availability date; valid dates, ~144 new house albums/week |
| `sort=-date` (avoid) | Surfaces corrupted-date old catalog entries first |
| Auth per request | Optional (Bearer token header) |
| Rate limits | None observed |
| Typical response time | ~120ms per request |
| Calls per genre per run | 1–3 (main + up to 2 pagination pages; capped at 3 pages per genre) |
| Data freshness | Near-real-time: albums live within 24h of release date |
| Error format | `{"message": "...", "code": "...", "request_id": "..."}` |
| HTTP method | GET only for browsing |

### Recommended query pattern

```
GET /api/v1/albums?sort=purchase&limit=50&offset=0&filter={"genres":[6],"release_start_from":"YYYY-MM-DD","curation":"curated"}
```

> Note: the `filter` value above is shown decoded for readability. In actual requests the JSON must be URL-encoded (see `_build_url` in Technical Notes).

`curation: "curated"` restricts to Volumo-vetted releases. Omit to fetch all. For TuneFinder the curated flag is recommended since it improves quality.

### Pagination strategy

Fetch pages until `len(items) < limit`. No sentinel value; the API just returns fewer items on the last page (or an empty list). A safe cap of 3 pages (150 items per genre) is reasonable for a 28-day window.

---

## 8. Listening and Auditioning Usefulness

| Question | Answer |
|---|---|
| Are track links useful for human listening? | Yes — `https://volumo.com/track/{id}-{slug}` opens a purchase/preview page |
| Audio preview available? | No — Volumo is a paid store, no free previews |
| Preview requires login? | n/a — no previews |
| Does link work without login? | Yes — track pages load publicly and show metadata |
| Useful for music digging? | Limited — you can see metadata, buy the track; no trial listen without purchase |
| Practical as a DJ dig tool? | Moderate — surfaces curated new releases but you cannot preview before buying |

**Note:** The lack of previews is a meaningful limitation for a DJ research tool. Users would need to find previews via Beatport, Soundcloud, or YouTube separately. However, the rich metadata (BPM, key, label, catalog number, ISRC) compensates for discovery and reference purposes.

---

## 9. Risk Assessment

| Risk | Level | Notes |
|---|---|---|
| API stability | Low–Medium | Not officially documented; reverse-engineered from JS bundle. API has been stable; version path is `/v1/`. |
| Auth/token risk | Low | Token is optional; browsing works unauthenticated. Rotation has no urgency. |
| Rate-limit risk | Low | No rate limiting observed in testing. |
| Paid-account restriction | Low | Browse API is public; purchase features require account but fetcher doesn't need them. |
| Metadata quality | Low | Rich, clean metadata for curated releases. |
| Stale data risk | Low | `release_start_from` filter ensures freshness. |
| Date corruption risk | Low (mitigated) | `sort=-date` returns corrupted entries; `release_start_from` + `sort=purchase` avoids them entirely. |
| Duplicate data risk | Low | Albums have stable integer IDs; dedup on `volumo_album_id` + `track_id`. |
| Source-link usefulness | Medium | Links are real but no free audio preview. |
| Account dependency risk | Low | No account required for metadata fetch. |
| `filter` JSON param fragility | Medium | Non-standard param format; could break if API changes query parsing. |
| Genre ID stability | Low | Integer IDs returned by `/api/v1/genres`; stable across sessions. |

---

## 10. Recommendation

**Add as disabled-by-default experimental source.**

Volumo offers a real REST API, clean metadata, stable genre IDs, and no authentication requirement. The curated catalog is high quality and tightly focused on electronic music sub-genres that TuneFinder cares about. The 28-day filter works correctly. The `albums_stats` endpoint enables efficient preflight checks.

The main limitation is the absence of audio previews, which reduces its utility as a solo discovery tool. As a complement to Beatport and Bandcamp it is a strong addition: it surfaces curated house, DnB, techno, breaks, UKG, and electronica releases with richer metadata than most sources (BPM, key, ISRC, catalog number).

---

## 11. Proposed Config Shape

```yaml
volumo:
  enabled: false              # disabled-by-default; enable when fetcher is implemented
  # API token is optional — browsing works unauthenticated
  # Set VOLUMO_API_KEY in .env for authenticated requests (future use)
  sort: purchase              # use 'purchase' not '-date' to avoid corrupted catalog entries
  curation: curated           # 'curated' = Volumo-vetted only; omit this key entirely for all releases — do NOT send curation:'all' or curation:null, omit the key from the filter JSON
  lookback_days: 28
  limit_per_genre: 50         # items per API call; paginate if count >= limit
  genres:
    # house and sub-genres — all tagged "house"
    - name: house
      id: 12                  # House
    - name: house
      id: 4                   # Deep House
    - name: house
      id: 21                  # Tech House
    - name: house
      id: 20                  # Soulful House
    - name: house
      id: 10                  # Funky / Jackin' House
    - name: house
      id: 15                  # Melodic House / Techno
    - name: house
      id: 19                  # Progressive House
    - name: house
      id: 1                   # Afro House
    # standalone genres
    - name: dnb
      id: 6                   # Drum and Bass
    - name: techno
      id: 22                  # Techno (Raw, Deep, Dub)
    - name: techno
      id: 23                  # Techno (Peak Time)
    - name: breaks
      id: 3                   # Breaks / Breakbeat
    - name: ukg
      id: 25                  # UK Garage / 2-Step
    - name: electronica
      id: 8                   # Electronica
    - name: downtempo
      id: 18                  # Organic House / Downtempo
```

The fetcher should read the API key from `os.environ.get("VOLUMO_API_KEY")` and pass it as `Authorization: Bearer {token}` if present. If absent, proceed without the header.

---

## 12. Acceptance Criteria for Follow-Up Implementation

- [ ] `fetch(settings, target_genre=None)` returns `list[SourceItem]`; config loaded via `settings.get_source_config("volumo")` (follows existing fetcher pattern)
- [ ] One API call per internal TuneFinder tag: group config genre entries by `name`, collect all unique `id` values per group, then issue one request per group with `"genres":[id1,id2,...]` (e.g. one call for `house` with `"genres":[12,4,21,20,10,15,19,1]`); deduplicate IDs within a group so the same Volumo ID is never sent twice
- [ ] `target_genre` filters the config genres list before issuing requests
- [ ] `release_start_from` is computed as today minus `lookback_days`
- [ ] Pagination follows this structure: `page = 0; while page < 3: fetch page; page += 1; if len(items) < limit: break` — the cap (`page < 3`) is checked before each fetch so exactly 1–3 fetches occur per tag; a full page-3 response does not trigger a page-4 fetch
- [ ] `curation` config value is serialized inside the `filter` JSON object (e.g. `{"genres":[...],"release_start_from":"...","curation":"curated"}`), not as a separate query parameter; if `curation` is absent or null in config, omit the key from the filter JSON entirely (do not send `"curation":null`)
- [ ] Track link constructed as `https://volumo.com/track/{id}-{slug}` where slug = `slugify("{title} {version}")` if version is non-null, else `slugify("{title}")`; if the API response includes a `slug` field on the track object, prefer it over the client-constructed slug
- [ ] `release_date` uses `release_start_at` from the track object, truncated to `YYYY-MM-DD`; validates `2020 <= year <= current_year + 1` before accepting (lower bound guards corrupted year-9 entries; upper bound guards far-future corrupted dates); if `release_start_at` fails the guard, fall back to the album's `first_live` timestamp (also truncated to `YYYY-MM-DD`) — **verify the exact field name in the live API response** (`first_live` is inferred from API inspection); if the fallback field is also absent or invalid, skip the track
- [ ] `release_name` uses `album.title`
- [ ] `genre_tags` uses the internal tag name from config (not Volumo genre name)
- [ ] `raw_metadata` includes: `volumo_track_id`, `volumo_album_id`, `bpm`, `keysign`, `version`, `isrc`, `catalog_number`, `duration_ms`, `label_name`, `label_id`
- [ ] API token read from `os.environ.get("VOLUMO_API_KEY")` (or a new `settings.volumo_api_key` property following the existing pattern in `src/config.py`); requests proceed without it if absent
- [ ] Source disabled by default in `settings.yaml`; `enabled: false` respected
- [ ] Graceful degradation on HTTP errors (log warning, return partial results)
- [ ] Unit tests with mocked HTTP responses
- [ ] `check-config` passes with `volumo.enabled: false`

---

## Technical Notes

### Track URL slug construction

```python
import re

def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)   # strip special chars; underscores survive here (part of \w)
    s = re.sub(r"[\s_]+", "-", s)    # convert spaces and surviving underscores to hyphens
    s = re.sub(r"--+", "-", s)       # collapse doubles
    return s.strip("-")

def _track_link(track_id: int, title: str, version: str | None, api_slug: str | None = None) -> str:
    if api_slug:
        return f"https://volumo.com/track/{track_id}-{api_slug}"
    parts = [title]
    if version:
        parts.append(version)
    slug = _slugify(" ".join(parts))
    return f"https://volumo.com/track/{track_id}-{slug}"
```

### Filter parameter construction

The `filter` query parameter takes a JSON-serialized object. Standard `requests` param encoding does not work for this endpoint — the filter must be passed as a raw URL-encoded JSON string:

```python
import json, urllib.parse

def _build_url(base: str, filter_obj: dict, sort: str, limit: int, offset: int) -> str:
    encoded_filter = urllib.parse.quote(json.dumps(filter_obj, separators=(",", ":")))
    return f"{base}?sort={sort}&limit={limit}&offset={offset}&filter={encoded_filter}"

# Correct usage — pass the fully-formed URL, no additional params=:
# url = _build_url("https://volumo.com/api/v1/albums", filter_obj, sort="purchase", limit=50, offset=0)
# resp = session.get(url, headers=headers, timeout=15)
# Do NOT use session.get(base, params={...}) — requests will re-encode the filter and corrupt it.
```

### Date validity guard

```python
from datetime import datetime, timezone

def _is_valid_date(date_str: str | None) -> bool:
    if not date_str:
        return False
    try:
        year = int(date_str[:4])
        current_year = datetime.now(timezone.utc).year
        return 2020 <= year <= current_year + 1
    except (ValueError, TypeError):
        return False
```

---

## Token Rotation Reminder

**Please rotate your Volumo API token.** The token in `.env` was used during this discovery session to confirm authenticated vs unauthenticated behaviour. Since browsing works without a token, the token is not required for the fetcher to function — but you may wish to rotate it as a precaution.

To rotate: log in to Volumo → account settings → API tokens → revoke and recreate. Update `VOLUMO_API_KEY` in your local `.env`.
