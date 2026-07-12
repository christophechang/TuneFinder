# Beatport API Migration — Design

Date: 2026-07-12
Status: Draft

## Goal

Restore Beatport as a working source by replacing the Cloudflare-blocked HTML/`__NEXT_DATA__` scraper with the Beatport internal **v4 API**. Behaviour is **like-for-like**: the fetcher still pulls the top-100 chart per configured genre and emits the same `SourceItem` shape (including `chart_position`), so dedup, history, scoring, and report rendering see no behavioural change. Layer in a few near-free `raw_metadata` enrichments that activate features already in the codebase (notably musical `key` → harmonic mixing).

Discovery + an end-to-end auth proof-of-concept were completed 2026-07-12 (see `project-beatport-cloudflare-block` memory). Official API access was denied by Beatport (support, March 2026); the user has explicitly accepted the ToS/account risk of using the internal API for personal, read-only use.

## Scope

`src/fetchers/beatport.py` (rewrite internals), new `src/fetchers/beatport_auth.py`, `src/config.py` (two optional env vars + Settings properties), `config/settings.yaml` (re-enable + drop the dead `chart_pattern`), `.env.example`, new `tests/test_beatport.py` + `tests/test_beatport_auth.py`, and `README.md` in several places (enumerated in §7). No new **runtime** dependencies — `requests` only; PKCE via stdlib (`hashlib`, `base64`, `secrets`). Token cache written to `data/` (already gitignored).

## Non-goals (deferred to their own changes)

- New-releases feed (`/catalog/tracks/?order_by=-publish_date`) — the API supports it; not this migration.
- Remix-aware track identity (issue #9) via `mix_name` — flag stays off pending validation.
- `is_hype` discovery scoring signal.
- `sample_url` direct audio previews (Beatport already has iframe embeds via `beatport_id`).
- Any change to dedup, ranker weights, history, pool, or Discord/report format.

## Current state (summary)

- `src/fetchers/beatport.py`: `fetch(settings, target_genre)` loads `sources.beatport` config, iterates genres, fetches each genre's `top-100` **website** page, extracts `__NEXT_DATA__`, parses tracks → `SourceItem`. Disabled since 2026-07-10 (`beatport.enabled: false`); genre list intact.
- Output contract (must be preserved): `SourceItem(source="beatport", artist, title, link, label, release_date, release_name, genre_tags, raw_metadata={"beatport_id", "bpm", "chart_position"})`.
- `_SLUG_TO_TAGS` maps Beatport genre slugs → internal tags (handles merged feeds + house sub-genres); per-track genre slug is read to tag merged feeds correctly. **Keep unchanged.**
- Fetcher pattern to mirror: `src/fetchers/volumo.py` (API source with an isolated `_get_json(url, session)` helper) and `tests/test_volumo.py` (mocks `_get_json`, `MagicMock` settings).
- Consumers of the enrichments already exist: `harmonic.py` reads `raw_metadata["keysign"]`/`["key"]` (verified: `to_camelot` parses Beatport `"Eb Major"`/`"D Minor"` with zero changes → `5B`/`7A`); `dedup.py` merges `bpm`/`key` across sources; `report_artifact.py`/`audition.py` display key; `ranker.py` turns `chart_position` into a discovery signal.

## Design

### 1. Auth/token module — `src/fetchers/beatport_auth.py`

Single public entry point:

```python
def get_access_token(settings) -> str   # raises BeatportAuthError on any failure
```

Returns a valid Bearer access token or raises `BeatportAuthError` (missing credentials **or** an auth failure). The fetcher lets that propagate so it lands in source health as a real error rather than a silent empty result (see §5). Behaviour:

1. **Credentials** — read `BEATPORT_USERNAME` / `BEATPORT_PASSWORD` (via Settings). If either is missing → raise `BeatportAuthError("credentials not set")`.
2. **One shared `requests.Session`** is created here and threaded through every network step below — login sets the `sessionid`/`csrftoken` cookies that the authorize step depends on, so the session must carry across login → authorize → token. Every helper (`_scrape_client_id`, `_login`, `_authorize`, `_exchange_token`, `_refresh`) **takes that session as a parameter**; none creates its own (a fresh session would silently drop the login cookies).
3. **Token cache** — `data/beatport_token.json`: `{access_token, refresh_token, expires_at, obtained_at}`, written atomically (`storage.atomic_write_json`), file mode `0600`. Load **validates shape** (a dict with a string `access_token`); a malformed cache (`[]`, `{}`, missing fields, unparseable) returns `None` and recovers via refresh/login rather than raising.
   - If cached `access_token` has > ~5 min left → return it (no network, no `client_id` needed).
   - Otherwise a refresh or login is required, so obtain the `client_id` (step 4) first, then:
     - if a `refresh_token` exists → `_refresh` (grant_type=refresh_token, **including `client_id`** — the token endpoint rejects a refresh without it; verified in the POC). On success rewrite cache — **preserving the existing `refresh_token` if the response omits a new one** (OAuth servers commonly do), so a refresh never nulls it and forces a premature full login — then return.
     - else / on refresh failure → full login (step 5).
4. **`client_id`** — scraped at runtime, **only when a refresh or login is actually needed**, from `https://api.beatport.com/v4/docs/` JS (regex over the referenced `static/btprt/*.js`). **Not hardcoded** — it rotates. The same scraped value feeds both `_refresh` and login, resolving the refresh path's dependency on it.
5. **Full login** (proven POC recipe, plain `requests`, no browser, on the shared session):
   - `POST /v4/auth/login/` JSON `{username, password}` → session cookies.
   - PKCE (`code_verifier`, `code_challenge` S256). `GET /v4/auth/o/authorize/` with `client_id`, `response_type=code`, `redirect_uri=https://api.beatport.com/v4/auth/o/post-message/`, `code_challenge`, `code_challenge_method=S256`, `state`, **no scope**, `allow_redirects=False` → `code` from the `Location`.
   - `POST /v4/auth/o/token/` grant_type=authorization_code (`code`, `redirect_uri`, `client_id`, `code_verifier`) → `access_token` (~10 h, `expires_in=36000`) + `refresh_token`. Write cache, return.
6. Every network step is a small helper (taking the shared session) so tests mock them. Failures are logged (`[beatport-auth]`) and raised as `BeatportAuthError`.

### 2. Beatport fetcher rewrite — `src/fetchers/beatport.py`

Same signature and output contract. `fetch(settings, target_genre)`:

1. If disabled → `[]` (unchanged).
2. `token = beatport_auth.get_access_token(settings)` — raises `BeatportAuthError` on missing creds or auth failure; the fetcher does **not** swallow it, so `fetch_all_sources` records a real source-health error (§5). Build a `requests.Session` with `Authorization: Bearer {token}`, `Accept: application/json`.
3. Genre selection + `target_genre` filtering: unchanged (reuse existing `_SLUG_TO_TAGS` logic).
4. Per genre: `GET /v4/catalog/genres/{id}/top/100/?per_page=100`, then **follow the response's own `next` URL** to collect up to 100 results (stop at `next=null`, an empty page, or 100). Following `next` rather than hand-building `page=N` is robust to whatever pagination scheme the endpoint uses and cannot re-fetch the same page into duplicates. `chart_position` = 1-based rank across the collected order. `polite_sleep` between genres (unchanged cadence).
5. Parse each track → `SourceItem` via `_parse_track`, preserving current fields and adding enrichments (§4):
   - `title` = `name`; `artist` = joined `artists[].name`; `link` = `https://www.beatport.com/track/{slug}/{id}`.
   - `label` = `release.label.name` (nested — top-level `label` is null); `release_name` = `release.name`; `release_date` = `publish_date` (fallback `new_release_date`).
   - `genre_tags` from per-track `genre.slug` via `_SLUG_TO_TAGS`, falling back to feed tags (unchanged).
6. Isolated `_get_json(url, session)` helper for mocking (mirrors volumo). Per-genre failures are logged and skipped, but the fetcher counts genres **attempted** vs. **completed** (a successful response counts as completed even if it yields zero tracks). After the loop:
   - ≥1 genre completed → return the collected items (partial is fine; may legitimately be `[]` if charts were genuinely empty).
   - genres attempted but **none** completed (total fetch failure) → raise `RuntimeError("beatport: all N genres failed")` so it surfaces as a source-health error (§5) rather than a silent empty result.
   - zero genres attempted (e.g. `target_genre` matched nothing) → return `[]`, no raise (matches volumo).

### 3. Config

- `.env.example`: add `BEATPORT_USERNAME=` and `BEATPORT_PASSWORD=`.
- `src/config.py`: add `Settings.beatport_username` / `Settings.beatport_password` properties (`os.getenv`). Do **not** register them in `_OPTIONAL_ENV_VARS` (its generic warning misrepresents an *enabled* source as "skipped"); instead emit one Beatport-specific warning in `validate()` when `sources.beatport.enabled` is true but the credentials are absent.
- `config/settings.yaml`: `beatport.enabled: true`; remove the now-dead `chart_pattern` line; keep the genre list verbatim (IDs verified 1:1 against the API). Update the disabled-source comment.

### 4. `raw_metadata` enrichments

`raw_metadata` becomes `{"beatport_id", "bpm", "chart_position", "key", "mix_name", "isrc"}`:
- `key` = track `key.name` (e.g. `"Eb Major"`) — **the one enrichment consumed today**: `harmonic.py` `candidate_camelot` reads `raw_metadata["key"]`, it's in the dedup key-merge field set, and `report_artifact.py`/`audition.py` display it. Primary win.
- `mix_name` (e.g. `"Original Mix"`, `"Primate Remix"`) and `isrc` — **captured but not consumed by the pipeline today** (verified: nothing reads them). Kept as future-facing metadata that persists into the archive/pool: `mix_name` for the deferred remix-identity work (issue #9), `isrc` for future cross-source dedup (its key name already matches Volumo's `isrc`). Not claimed as displayed or as Volumo-metadata parity.
- `length_ms` is **dropped** — no consumer and no planned use (YAGNI). Easy to add later if a need appears.

### 5. Failure handling & source health

The run never crashes — `fetch_all_sources` wraps each fetcher in try/except, records `{count, error}` per source, and continues with the others. Given that, we deliberately distinguish the failure modes so `source_health.py` reports them correctly (it alerts `"beatport: FAILED — {error}"` when `error` is set, but only `"0 items…"` when `count==0` with no error):

- **Enabled but broken** (missing credentials, auth failure, or total fetch failure — every *attempted* genre request errored, per §2.6) → let `BeatportAuthError`/`RuntimeError` propagate; the aggregator records `error=<message>` → surfaced as a clear `FAILED` alert. Deliberately **not** a silent `[]`, so a broken login is diagnosable instead of masquerading as "0 items". (Design choice: an *enabled* source that can't authenticate is a misconfiguration worth flagging, so missing creds raises too — happy to make missing-creds a silent skip instead if preferred.)
- **Per-genre fetch error** → logged and skipped; the other genres' results are still returned (partial), matching `volumo`.
- **Disabled** → skipped by `fetch_all_sources` before the fetcher is called (unchanged).

Graceful degradation (run continues, Discord report still posts) is preserved; the only change is that a Beatport failure is now *visible* in health/alerts rather than silent.

### 6. Testing

- `tests/test_beatport.py` — mirror `test_volumo.py`: mock `_get_json` and `get_access_token`; `MagicMock` settings. Cover: disabled→`[]`; parse → correct `SourceItem` incl. `chart_position`, `label` from `release.label`, `link` format; `raw_metadata` enrichments (`key`, `mix_name`, `bpm`, `isrc`); `target_genre` filter; merged-feed per-track genre tagging; pagination-to-100; per-genre error → partial results (other genres still returned); **all-genres-fail → `fetch` raises `RuntimeError`** (total-failure contract); `get_access_token` raising `BeatportAuthError` propagates out of `fetch` (so the aggregator records the error).
- `tests/test_beatport_auth.py` — mock the network helpers (each takes the shared session): cached-token reuse (no network, no `client_id` scrape); refresh path (**asserts `client_id` is sent**); re-login when refresh fails; `client_id` scrape parsing; missing-creds → raises `BeatportAuthError`; login failure → raises `BeatportAuthError`; cache written with mode `0600`.
- Validate: `./venv/bin/python -m tunefinder check-config`, `./venv/bin/pytest tests/ -v`, plus a live `--dry-run` once credentials are in `.env`.

### 7. Documentation (`README.md`)

Every Beatport touchpoint, not just the Sources table:
- **Sources table** — the `__NEXT_DATA__` / "blocked (Cloudflare…)" row → "Genre top-100 chart (v4 API)" / active.
- **Fetch overview** ("scrapes new releases from Beatport…") → reword; it's an authenticated API now, not a scrape.
- **Environment variables** example (near `VOLUMO_API_KEY=`) → add `BEATPORT_USERNAME=` / `BEATPORT_PASSWORD=` with a one-line note (unofficial API; personal use).
- **Setup / architecture file-tree** entry (`beatport.py # … __NEXT_DATA__`) → API description; add `beatport_auth.py`.
- **mix-prep BPM/key note** ("Beatport: `bpm`") → now `bpm` + `key`.
- **breaks/uk-bass combined-feed note** ("from the page data") → "from the API".

## Risks & considerations

- **Unofficial API / ToS** — accepted by the user (personal, read-only, low-volume, replaceable account). Mitigations: read-only public catalog only; existing `polite_sleep` cadence; graceful degradation.
- **Token cache secrecy** — `data/beatport_token.json` holds a refresh token; `data/` is gitignored; write mode `0600`.
- **`client_id` rotation** — handled by scraping at runtime (never hardcoded).
- **To confirm during implementation** (low-risk): that following `next` yields ~100 tracks per genre and terminates cleanly (the code is scheme-agnostic, so this is a dry-run sanity check, not a code branch); that `redirect_uri`/no-scope remain accepted (POC-verified 2026-07-12).
- **Snapshot tests** — output contract is unchanged and new `raw_metadata` keys are additive, but `report_artifact.py`/`audition.py` surface `key`; confirm no deterministic snapshot shifts, and if any are intended, update snapshots deliberately.

## Rollout

Land on `develop` with tests green. Re-enable in `settings.yaml`; add credentials to `.env` locally and on the prod host. Dry-run, then a normal run; confirm Beatport health count > 0. Follows the standard "deploy release" flow.
