TuneFinder fetches music releases, scores them against a DJ profile, generates a report via LLM cascade, and posts to Discord. Codebase is intentionally small and script-like. Clarity and continuity over restructuring.

Inspect code before making assumptions. Match existing patterns. Prefer config-driven changes over hardcoded constants. No hardcoded secrets, IDs, tokens, or personal URLs. Treat `.env`, `data/`, `logs/`, `fixtures/` as important local state — do not wipe or rewrite unless asked. Do not enable disabled sources (Boomkat, Bleep) without approval. Preserve graceful degradation for missing API keys, fetch failures, and LLM fallback behavior. Networked fetchers are brittle — prefer narrow parsing updates over broad rewrites.

Follow current style: straightforward functions, dataclasses, explicit control flow, lightweight modules. Reuse `src/fetchers/common.py` and existing pipeline utilities. Keep LLM provider logic in `src/llm.py`. Keep Discord formatting in `src/pipeline/report.py`. Be careful around recommendation history and candidate pool — regressions cause duplicate recommendations.

Key paths:
- `tunefinder/__main__.py` — CLI commands and run orchestration
- `src/config.py` — env/config loading and validation
- `src/models.py` — shared dataclasses and canonical record shapes
- `src/fetchers/` — source-specific scraping and ingestion
- `src/pipeline/` — ranking, dedup, history, reporting, pool/profile logic
- `src/llm.py` — LLM provider cascade
- `config/settings.yaml` — source toggles, model config, pipeline counts, channel names

Validation: `./venv/bin/python -m tunefinder check-config` first. Use `--dry-run` for pipeline changes. Never post live Discord messages unless explicitly asked. If validation is blocked by missing credentials or side-effect risk, say so.

If a change affects commands, config keys, or operator workflow, update `README.md` in the same pass.