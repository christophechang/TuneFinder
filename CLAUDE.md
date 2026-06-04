TuneFinder fetches music releases, scores them against a DJ profile, generates a report via LLM cascade, and posts to Discord. The codebase favours straightforward, focused modules — but it is a real application, not a throwaway script. Add tests, helpers, and dev tooling where they earn their keep. Don't restructure for its own sake.

Inspect code before making assumptions. Match existing patterns. Prefer config-driven changes over hardcoded constants. No hardcoded secrets, IDs, tokens, or personal URLs. Treat `.env`, `data/`, `logs/`, `fixtures/` as important local state — do not wipe or rewrite unless asked. Do not enable disabled sources (Boomkat, Bleep) without approval. Preserve graceful degradation for missing API keys, fetch failures, and LLM fallback behavior. Networked fetchers are brittle — prefer narrow parsing updates over broad rewrites.

Follow current style: straightforward functions, dataclasses, explicit control flow, lightweight modules. Reuse `src/fetchers/common.py` and existing pipeline utilities. Keep LLM provider logic in `src/llm.py`. Keep Discord formatting in `src/pipeline/report.py`. Be careful around recommendation history and candidate pool — regressions cause duplicate recommendations.

Dev dependencies (test framework, mocks, linters) may be added to `requirements-dev.txt` without re-asking. Runtime dependencies in `requirements.txt` still need approval before adding.

Key paths:
- `tunefinder/__main__.py` — CLI commands and run orchestration
- `src/config.py` — env/config loading and validation
- `src/models.py` — shared dataclasses and canonical record shapes
- `src/fetchers/` — source-specific scraping and ingestion
- `src/pipeline/` — ranking, dedup, history, reporting, pool/profile logic
- `src/llm.py` — LLM provider cascade
- `config/settings.yaml` — source toggles, model config, pipeline counts, channel names
- `tests/` — pytest suite mirroring `src/` layout; mock external IO (LLM HTTP calls, Discord)

Validation: `./venv/bin/python -m tunefinder check-config` first. Run tests with `./venv/bin/pytest tests/ -v`. Use `--dry-run` for pipeline changes. New behavior should ship with tests. Never post live Discord messages unless explicitly asked. If validation is blocked by missing credentials or side-effect risk, say so.

If a change affects commands, config keys, or operator workflow, update `README.md` in the same pass.

When user says **"deploy release"**:

1. **Determine version** — inspect `CHANGELOG.md` for the next version to use (or decide based on unreleased changes: patch/minor/major).

2. **Update CHANGELOG.md** — gather all changes that are either:
   - Uncommitted (working tree / staged), or
   - Committed but not yet pushed to `origin/develop`
   Add them under a new version header with today's date.

2. **Update README.md** — check readme to check for stale / incorrect details. If inconsistencies found, update the file.

3. **Commit changelog** — stage and commit `CHANGELOG.md` (and any other uncommitted changes) with message `chore: prepare vX.Y.Z release`.

4. **Push develop** — `git push origin develop`.

5. **Tag the release** — `git tag vX.Y.Z` then `git push origin vX.Y.Z`.

6. **Deploy to production server** — SSH as `christophechang@192.168.1.122` and run:
   ```bash
   cd /Users/christophechang/OpenClaw/Automations/TuneFinder && git checkout main && git pull origin main
   ```
   Confirm the pull succeeded and the working tree is clean before reporting done.
