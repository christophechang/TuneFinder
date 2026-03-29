# Claude Code Guidance For TuneFinder

## Project Overview
- TuneFinder is a Python automation project that fetches music releases, scores them against a DJ profile, generates a report with an LLM cascade, and posts to Discord.
- The codebase is intentionally small and script-like. Prefer clarity and continuity over framework-style restructuring.

## Repo-Specific Expectations
- Inspect the code before making assumptions. Match existing patterns in `src/`, `tunefinder/`, and `config/`.
- Keep changes minimal and local to the requested task.
- Do not refactor unrelated modules just because a cleaner design is possible.
- Prefer config-driven changes over hardcoded constants.

## Important Paths
- `tunefinder/__main__.py`: CLI commands and run orchestration
- `src/config.py`: environment/config loading and validation
- `src/models.py`: shared dataclasses and canonical record shapes
- `src/fetchers/`: source-specific scraping and ingestion
- `src/pipeline/`: ranking, deduplication, history, reporting, pool/profile logic
- `src/llm.py`: LLM provider cascade logic
- `config/settings.yaml`: source toggles, model config, pipeline counts, channel names

## How To Work Safely Here
- Do not hardcode secrets, IDs, tokens, or personal URLs.
- Treat `.env`, `data/`, `logs/`, and `fixtures/` as important local state. Do not wipe or rewrite them unless explicitly asked.
- Networked fetchers are brittle by nature. For scraper fixes, prefer narrow parsing updates over broad rewrites.
- Do not enable disabled sources like Boomkat or Bleep without explicit approval.
- Preserve graceful degradation for missing API keys, fetch failures, and LLM fallback behavior.

## Implementation Preferences
- Follow the current Python style: straightforward functions, dataclasses, explicit control flow, and lightweight modules.
- Reuse shared helpers in `src/fetchers/common.py` and existing pipeline utilities instead of duplicating behavior.
- Keep provider-specific LLM behavior centralized in `src/llm.py`.
- Keep Discord formatting and sanitization logic centralized in `src/pipeline/report.py`.
- Be especially careful around recommendation history and candidate pool behavior; regressions there can create duplicate recommendations.

## Validation
- Prefer safe validation paths first.
- Start with:
- `./venv/bin/python -m tunefinder check-config`
- Use `./venv/bin/python -m tunefinder run --dry-run` for end-to-end checks when the change affects pipeline behavior.
- Avoid commands that post live messages to Discord unless the user explicitly requests that.
- If validation is blocked by missing credentials, network access, or side-effect risk, say so plainly.

## Dependencies And Scope
- Do not add dependencies without approval.
- Do not introduce large abstractions, framework migrations, or broad file moves unless explicitly requested.
- If a change affects commands, config keys, or operator workflow, update `README.md` in the same pass when appropriate.

## Collaboration Notes
- Explain non-obvious tradeoffs briefly before making high-impact changes.
- Call out assumptions when runtime behavior cannot be verified locally.
- Optimize for maintainability by the current repo owner, not for theoretical extensibility.
