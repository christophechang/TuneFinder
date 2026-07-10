# TuneFinder Web — Architecture

**Status:** Source of truth for the web transformation (2026-07).
**Repos:** [`TuneFinder`](https://github.com/christophechang/TuneFinder) (engine + API), [`tunefinder-web`](https://github.com/christophechang/tunefinder-web) (SPA).

## 1. Product

TuneFinder becomes a web application for its one user: a DJ who digs weekly and
preps sets. The web app replaces the *interactive* surfaces that today are CLI
homework and static HTML:

- **Report browsing** — weekly and mix-prep reports with inline audition
  players (Bandcamp/Beatport embeds), deterministic reasons, signal badges,
  BPM/key, and store links. The static audition page's job, done properly.
- **One-tap feedback** — `bought / liked / skip / own` buttons on every track
  (today: copy a CLI command to a clipboard, walk to a terminal). Closing the
  feedback loop is the single highest-leverage product change: the scoring
  engine already consumes feedback, but capture friction keeps the data thin.
- **Mix-prep workbench** — the hero feature. Genre, BPM range, Camelot key
  wheel, dry-run toggle; live run progress; results land as an interactive
  report.
- **On-demand runs** — trigger weekly or mix-prep runs from anywhere, with
  progress and log tail. The Sunday launchd run stays; the web adds agency.
- **Insights** — feedback stats and per-signal lift (`tune-report`), genre
  affinity, label affinity memory, candidate pool, source health history.
- **Explain** — the pipeline trace (`tunefinder explain`) as a readable
  timeline: fetched → dedup → filters → scoring → section → pool → feedback.

Discord stays as the *push* channel (weekly report lands there; report links
into the web app). The web app is the *pull* channel and the action surface.

**Non-goals:** multi-user/tenancy, catalogue management, new sources, scoring
changes. The engine's behaviour is untouched; the web is a new skin over the
same deterministic pipeline.

## 2. Shape: API on the Mac mini, SPA on the edge

```
Browser ── SPA (tunefinder-web, Cloudflare Pages or LAN)
   │  HTTPS + Bearer secret
   ▼
FastAPI service (TuneFinder repo, Mac mini, launchd)
   │  direct library calls — no queue, no subprocess
   ▼
Existing pipeline modules  ──  data/*.json on the mini
   │
   └─ fetchers (residential IP), Discord, catalog API (api.changsta.com)
```

**Deliberate divergence from MixLab Anywhere.** MixLab routes jobs through an
Azure blob queue to a mini worker because its system-of-record (uploads, run
artifacts, feedback) lives in Azure and each run is stateless. TuneFinder is
the opposite: the system-of-record (history, feedback, pool, profiles,
archives) *is* the mini's `data/` directory, the fetchers need the residential
IP, and most web interactions are sub-second reads/writes of that local state.
Routing a feedback mark or an explain trace through a cloud blob queue would
add three contract surfaces (TS/C#/Python), an Azure dependency, and seconds
of latency to interactions that are function calls away from the data. The
API therefore runs *next to the data* and imports the pipeline directly.

What we keep from MixLab: the SPA stack and conventions (Vite + React + TS +
Tailwind, dark-first, single accent, settings page with base URL + bearer
secret in localStorage, polling with visibility-aware refetch, Cloudflare
Pages deploy with `_redirects`). What we fix from its post-mortem: the API
contract lands first and is generated (OpenAPI → TS types, not hand-mirrored),
artifacts are structured JSON (no stdout parsing), both repos get CI, and the
settings gate gets a "save without test" escape hatch.

**Exposure is the operator's choice, not the app's assumption.** The service
binds to localhost/LAN. For access from anywhere, run an outbound-only
Cloudflare Tunnel (`cloudflared`) in front — same "no inbound connections to
the home network" property MixLab's queue provides, without the queue — and
optionally Cloudflare Access. The app is correct on plain LAN, Tailscale, or a
tunnel; auth (below) is enforced regardless.

## 3. Backend (TuneFinder repo)

New additive layers; existing CLI behaviour byte-identical (snapshot tests
guard it).

- `src/services/runs.py` — orchestration extracted from `cmd_run` /
  `cmd_mix_prep` into callable services: `run_weekly(settings, options) ->
  RunOutcome`, `run_mix_prep(settings, options) -> RunOutcome`, with a
  progress callback (stage events) and the same dry-run gating. The CLI
  handlers become thin wrappers over these services.
- **Structured report artifact** — every live run writes
  `data/reports/report_{report_id}.json` next to the audition HTML: full
  sections with per-track reason text, signals (code + explanation), scores
  (combined/familiarity/discovery), BPM/key, embed ids from `raw_metadata`,
  funnel stats, filters applied. This is the web-native `summary.json`
  equivalent — the SPA renders reports from it. Older reports (pre-artifact)
  render degraded from history records.
- **Write safety** — one shared helper does atomic writes (temp file +
  `os.replace`) for every JSON store, and a `data/`-scoped inter-process file
  lock serialises pipeline runs and store mutations across the web service,
  the launchd CLI run, and manual CLI use. A web-triggered run while the
  Sunday run is executing returns 409.
- `src/web/` — FastAPI app: `app.py` (factory, CORS from config),
  `auth.py` (Bearer secret from `TUNEFINDER_API_SECRET`, constant-time
  compare, fail-closed: the server refuses to start without a secret unless
  `TUNEFINDER_WEB_INSECURE=1`), `jobs.py` (in-process job registry — one
  pipeline run at a time, status + stage progress + log tail, recent-job
  history persisted to `data/web_jobs.json`), `schemas.py` (Pydantic
  response models → OpenAPI), route modules per resource.
- **API surface (v1, all under `/api`):**
  - `GET /health` — service + data freshness + source health summary (no auth)
  - `GET /reports?kind=weekly|mix-prep` · `GET /reports/{report_id}` — feedback state joined per track
  - `POST /feedback` — `{key|track_no+report_id, outcome}` (replaces `mark`)
  - `GET /feedback/stats` — `stats` + `tune-report` data
  - `GET /explain?selector=` — structured trace
  - `POST /runs` — `{mode, genre?, bpm?, key?, bpm_flex?, dry_run?}` → 202 job id, 409 if busy
  - `GET /runs` · `GET /runs/{job_id}` — status, stages, log tail, result report id
  - `GET /profile` — top artists (raw + recency-weighted), genre affinity, label affinity
  - `GET /pool` — candidate pool view
  - `GET /sources/health` — per-source run history
  - `GET /config` — sanitised settings (weights, sources, counts; no secrets)
- `tunefinder serve` CLI command runs uvicorn; optional static mount serves a
  built SPA bundle (`TUNEFINDER_WEB_STATIC_DIR`) for zero-CORS LAN use.
- New runtime deps: `fastapi`, `uvicorn` (dev: `httpx` for TestClient).
  *Assumption recorded:* the transformation brief authorises these runtime
  additions despite the repo's default ask-first rule.

## 4. Frontend (tunefinder-web repo)

Vite + React 18 + TypeScript strict + Tailwind + react-router + vitest —
mixlab-web conventions, TuneFinder's own identity (dark-first zinc, violet
accent, the "night radar" of the two products). No component framework; small
tested primitives. API types generated from the backend's OpenAPI spec into
`src/api/types.gen.ts` (checked in, regenerated by script).

Routes: `/` dashboard · `/reports` + `/reports/:id` · `/mix-prep` ·
`/runs/:jobId` · `/insights` · `/explain` · `/settings`. All but `/settings`
gated on configured connection (with "save without test" escape hatch).

Deploy: Cloudflare Pages (`_redirects` SPA fallback) or the backend's static
mount. CI: GitHub Actions — lint, typecheck, tests, build on PR/push (both
repos; fixes MixLab's no-CI wart).

## 5. Operational model

- Weekly Sunday run: unchanged launchd CLI job (now takes the run lock).
- Web service: launchd `KeepAlive` job on the mini running `tunefinder serve`
  (runbook in `docs/ops/`).
- Discord report gains a link to the web report
  (`TUNEFINDER_WEB_BASE_URL`), superseding the audition-page link when set.
- Data stays JSON-on-disk. At n=1 with a run lock and atomic writes this is
  honest and sufficient; a datastore migration is deliberately out of scope.

## 6. Reconciliation with the existing roadmap

- TuneFinder #9 (remix-aware identity) — merged, open pending home
  validation. Unaffected; the web app renders whatever identity the engine
  uses. Stays open.
- TuneFinder #10 (label-roster sourcing) — open, needs home-network spike.
  Orthogonal to the web work. Stays open.
- Web milestones are tracked as new issues: M1–M3 in TuneFinder (services +
  artifact, API, jobs), M4–M7 in tunefinder-web (scaffold, reports+feedback,
  mix-prep+runs, dashboard+insights+explain), M8 integration/ops/docs.
