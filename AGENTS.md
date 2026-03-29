# TuneFinder Repo Instructions

## Purpose
- TuneFinder is a Python 3.11+ automation app for weekly music discovery.
- It fetches releases from multiple external sources, ranks them against a DJ taste profile, generates an LLM-written report, and posts to Discord.
- Prefer small, targeted changes that preserve the existing pipeline shape and CLI workflow.

## Architecture
- `tunefinder/__main__.py` is the CLI entrypoint. New workflows should usually be exposed here only if they are true user-facing commands.
- `src/config.py` loads YAML settings and environment variables. Prefer configuration changes over hardcoding.
- `src/models.py` contains the core dataclasses shared across fetchers and pipeline stages.
- `src/fetchers/` contains one module per source. Keep source-specific scraping logic isolated to its own module.
- `src/pipeline/` contains profile building, deduplication, ranking, history, pool management, and report generation.
- `src/llm.py` owns the provider cascade logic. Keep provider-specific behavior centralized here.
- `src/output/discord.py` handles Discord posting. Avoid mixing posting logic into pipeline modules.

## Working Style
- Inspect the existing module before changing behavior. Follow established naming, data flow, and logging patterns.
- Prefer simple functions and incremental edits over broad refactors.
- Do not introduce new abstractions unless repeated duplication clearly justifies them.
- Keep changes tightly scoped to the user request. Do not opportunistically reorganize unrelated modules.
- Preserve current import style (`from src...`) and the existing dataclass-driven model layer.

## Configuration And Secrets
- Never hardcode API keys, Discord IDs, tokens, or user-specific URLs.
- Treat `.env`, `data/`, `logs/`, and `fixtures/` as runtime/stateful artifacts. Do not delete or rewrite them unless the task explicitly requires it.
- Prefer changing defaults in `config/settings.yaml` or `src/config.py` rather than scattering constants through the codebase.
- If a change affects provider requirements, fallback chains, or channel names, update the relevant config and README together when appropriate.

## Fetchers And External Integrations
- This repo depends on fragile third-party HTML/JSON structures. In fetchers, make the smallest resilient fix possible.
- Preserve source-specific headers, throttling, and parsing helpers in `src/fetchers/common.py` unless the task clearly requires changing them.
- When a source is blocked or disabled, do not force-enable it without explicit instruction.
- Fail gracefully on external errors and keep logging informative.

## LLM And Reporting
- Keep the two-stage report pipeline intact unless the task explicitly asks to redesign it.
- Stage 1 enriches reasons; Stage 2 writes the final report. Avoid duplicating cascade logic outside `src/llm.py`.
- Maintain Discord-safe formatting behavior in `src/pipeline/report.py`, especially link sanitization and embed suppression.
- If changing prompts or report formatting, preserve deterministic fallbacks when LLM calls fail.

## Data Safety
- Recommendation history and pool files prevent repeat recommendations. Be careful with any logic touching `src/pipeline/history.py` or pool persistence.
- Avoid changes that could silently re-surface already recommended tracks unless that is the requested behavior.
- Keep dry-run behavior safe: no Discord posts with side effects beyond what the code already documents, and no history mutations.

## Verification
- Prefer the least destructive validation that fits the change.
- Useful commands:
- `./venv/bin/python -m tunefinder check-config`
- `./venv/bin/python -m tunefinder run --dry-run`
- `./venv/bin/python -m tunefinder mix-prep <genre>`
- Avoid live runs that post to Discord unless the user explicitly asks for them.
- If you cannot fully verify due to missing credentials, network limits, or side-effect risk, state that clearly.

## Dependencies
- Do not add or upgrade dependencies without explicit approval.
- Prefer standard library or existing packages already listed in `requirements.txt`.

## Documentation
- Update `README.md` when changing user-facing commands, setup steps, configuration keys, or pipeline behavior.
- Keep docs concrete and aligned with the actual CLI and config files in this repo.

## Commits
- Use conventional commits when committing.
- Do not add `Co-Authored-By` trailers.
