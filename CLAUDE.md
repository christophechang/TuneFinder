TuneFinder fetches music releases, scores them against a DJ profile, generates a deterministic report, and posts to Discord. The codebase favours straightforward, focused modules — but it is a real application, not a throwaway script. Add tests, helpers, and dev tooling where they earn their keep. Don't restructure for its own sake.

Inspect code before making assumptions. Match existing patterns. Prefer config-driven changes over hardcoded constants. No hardcoded secrets, IDs, tokens, or personal URLs. Treat `.env`, `data/`, `logs/`, `fixtures/` as important local state — do not wipe or rewrite unless asked. Do not enable disabled sources (Boomkat, Bleep) without approval. Preserve graceful degradation for missing API keys and fetch failures. Networked fetchers are brittle — prefer narrow parsing updates over broad rewrites.

Follow current style: straightforward functions, dataclasses, explicit control flow, lightweight modules. Reuse `src/fetchers/common.py` and existing pipeline utilities. Report rendering is deterministic — reasons in `src/pipeline/reasons.py`, layout in `src/pipeline/report.py`. Keep rendering free of network/LLM dependencies. Snapshot tests guard exact output; update them deliberately, never casually. Keep Discord formatting in `src/pipeline/report.py`. Be careful around recommendation history and candidate pool — regressions cause duplicate recommendations.

Dev dependencies (test framework, mocks, linters) may be added to `requirements-dev.txt` without re-asking. Runtime dependencies in `requirements.txt` still need approval before adding.

Key paths:
- `tunefinder/__main__.py` — CLI commands and run orchestration
- `src/config.py` — env/config loading and validation
- `src/models.py` — shared dataclasses and canonical record shapes
- `src/fetchers/` — source-specific scraping and ingestion
- `src/pipeline/` — ranking, dedup, history, reporting, pool/profile logic
- `src/services/runs.py` — run orchestration shared by CLI and web API
- `src/web/` — FastAPI web service (`tunefinder serve`); schemas.py is the OpenAPI contract tunefinder-web generates types from
- `config/settings.yaml` — source toggles, pipeline counts, channel names
- `tests/` — pytest suite mirroring `src/` layout; mock external IO (Discord)

Validation: `./venv/bin/python -m tunefinder check-config` first. Run tests with `./venv/bin/pytest tests/ -v`. Use `--dry-run` for pipeline changes. New behavior should ship with tests. Never post live Discord messages unless explicitly asked. If validation is blocked by missing credentials or side-effect risk, say so.

CI (`.github/workflows/ci.yml`) runs `pytest` on pushes and PRs to `main`/`develop`, on Python 3.11 to match production, plus nightly at 06:00 UTC — the suite has date-dependent fixtures that can rot with no commit behind them. It does **not** run `check-config`: that needs real credentials and stays a local pre-release step. Releases merge `--ff-only` locally rather than through a PR, so CI reports on the resulting push rather than gating the merge — check it after a release, not before.

If a change affects commands, config keys, or operator workflow, update `README.md` in the same pass.

## Releasing a version

When the operator says **"deploy release"**: run the user-level `deploy-release` skill (changelog → README
staleness check → verify → commit `chore: prepare vX.Y.Z release` → push `develop` → merge `main` → tag →
GitHub release → back to `develop`). Repo specifics:

- `main` is the production trunk — production pulls `origin/main`. Merge with `git merge --ff-only develop`;
  if the fast-forward fails (main has commits develop lacks), stop and reconcile — never force.
- Tag `vX.Y.Z` on `main`. Create the GitHub Release with `--verify-tag --latest`, using the new
  `CHANGELOG.md` section as notes (write to a temp file, pass via `--notes-file`).
- Deploy: SSH `christophechang@192.168.1.122`, then
  `cd /Users/christophechang/OpenClaw/Automations/TuneFinder && git checkout main && git pull origin main`
  — confirm the pull succeeded and the working tree is clean.
- Restart the web service — the resident `tunefinder serve` process imports code once at boot, so a pull
  alone silently serves stale code to web-triggered runs (`POST /api/runs`):
  `launchctl kickstart -k gui/$(id -u)/com.openclaw.tunefinder-web` (gui-domain LaunchAgent, no sudo).
- Health check before reporting done (run on the prod box via the same SSH session — the service
  binds locally): `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8420/api/health` → expect `200`.
- If the change touched `src/web/schemas.py`, confirm the deployed spec agrees before reporting done:
  `curl -s https://tunefinder.changsta.com/openapi.json` and check the field you added is present.

### Changes that span this repo and tunefinder-web

The same LaunchAgent serves this API and the SPA's `dist/`, but the two repos release separately.
When a change needs both (a new API field, a new `Embed` type, anything the SPA's
`npm run generate-types` consumes):

- **Ship tunefinder-web first, this repo second.** A stale SPA against a new backend can render
  wrongly — a new `Embed` type, for instance, makes the old bundle draw an empty player slot,
  because its `PlayerEmbed` returns `null` for types it doesn't know. The reverse is merely inert.
- The SPA regenerates its types from a *locally* running backend on this branch
  (`TUNEFINDER_WEB_INSECURE=1 ./venv/bin/python -m tunefinder serve`, then `npm run generate-types`
  there). `TUNEFINDER_WEB_INSECURE=1` binds 127.0.0.1 only and avoids needing the production bearer.
- Adding a member to an existing `Literal` is additive and safe: persisted artifacts holding only
  the older values still validate.

### Pipeline changes do not touch existing reports

`build_report_artifact` is pure and runs once per pipeline run; its output is frozen into
`data/reports/report_*.json`, and `src/web/reportdata.py` replays those files verbatim. **Changing
how `_embed`, `signals` or `reason` are built has no effect on reports that already exist** — only
on ones generated afterwards.

Expect "deployed, but the reports look the same" and check the stored artifact before debugging
anything. Seeing the change means a fresh pipeline run, which does live fetching and can post to
Discord — that is the operator's decision, never an implicit part of a deploy.
