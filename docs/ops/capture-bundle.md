# Capture bundles — golden fixtures for the engine port

`run --dry-run --capture-bundle DIR` freezes one weekly run into a directory that
another engine can be proven against. `replay --bundle DIR` re-runs this engine over
that directory alone and checks it reproduces its own report artifact byte for byte.
The multi-tenant product uses bundles as the golden fixtures for its .NET port of the
ranker (tunefinder-multi-tenant `docs/CONTRACTS.md` §6, SPIKES S2).

This is the one change the personal tool takes for the product. A normal `run`, dry or
live, is unchanged: the bundle code runs only when `--capture-bundle` or `--bundle` is
given.

## Capture

```bash
./venv/bin/python -m tunefinder run --dry-run --capture-bundle ~/bundles/2026-W39
```

- **Dry runs only.** `--capture-bundle` without `--dry-run` is refused, and so is a
  `DIR` that exists and is not empty.
- **It writes only into `DIR`.** A plain `--dry-run` is not write-free: it saves
  `source_items.json`, archives a snapshot (pruning the archive to 26) and rewrites
  `known_tracks.json`, `artist_profiles.json` and `genre_affinity.json`. A capture run
  sends all five of those writes to `DIR` or skips them (the archive). Four side
  effects outside `DIR` remain, because every run has them: the run lock in the data
  dir, SoundCloud and Beatport token refreshes in the data dir, and the day's log file
  under `logs/`.
- **It bundles what was consumed.** Once the run holds the lock, each data-dir input is
  copied into `DIR`, and the engine then reads it *from `DIR`*. A mark saved from the
  web app halfway through the run can't split the bundle from what was scored.
- **Its clock is frozen** at the moment the run starts, and the bundle records it. The
  report id, `generated_at`, the release window, pool age and the label-memory cutoff
  all read that one instant.

Capture needs the same credentials as any run: it fetches the catalog and every
enabled source live.

## What a bundle holds

| File | What it is |
|---|---|
| `settings.yaml`, `aliases.yaml` | The config the run loaded (`aliases.yaml` only if one exists) |
| `feedback.json`, `label_affinity.json`, `recommendation_history.json`, `mix_prep_history.json`, `candidate_pool.json`, `learned_weights.json` | The data-dir inputs as loaded. The pool is from before the rebuild and the histories are from before the append, because a dry run does neither. An input missing from the data dir is missing from the bundle too. |
| `known_tracks.json`, `artist_profiles.json`, `genre_affinity.json` | The profile state the engine scored with: freshly built from the catalog, or the cached copy on the degraded path (`manifest.used_fallback`) |
| `source_items.json`, `fetcher_health.json` | The fetched corpus and per-source health (the artifact's `stats` embeds the health) |
| `learning.json` | `loaded` (learned weights as read), `updated` (after this run's update), `multipliers` (what the ranker was actually handed), `tune_data`, `adjustments` |
| `manifest.json` | `format`, `kind`, `clock`, `report_id`, `engine_commit` (`sha`, `dirty`), `used_fallback`, `remix_aware`, `seed_queries`, `no_candidates`, `files` |
| `report_artifact.json` | The report artifact, serialised exactly as `data/reports/` stores it. Absent if the week had no candidates. |

## Replay

```bash
./venv/bin/python -m tunefinder replay --bundle ~/bundles/2026-W39
./venv/bin/python -m tunefinder replay --bundle ~/bundles/2026-W39 --out /tmp/replayed.json
```

Replay runs the same `run_weekly` code in dry-run mode:

- It reads the settings, aliases, inputs, corpus and clock from the bundle.
- It fetches nothing and takes no run lock.
- It never loads `config/settings.yaml` or the live data dir.
- It writes only to `--out`, when given.

It prints `MATCH` with the artifact's SHA-256 and exits 0. In three cases it prints
`MISMATCH`, lists each difference and exits 1:

- the replayed artifact differs from `report_artifact.json` by even one byte (the first
  differing line is shown);
- the recomputed learning state differs from `learning.json`;
- the report id differs.

`--week` and `--bundle` are exclusive. `--set` applies only to `--week`: a bundle
replays exactly as captured.

A mismatch means the capture is incomplete or the engine is not deterministic over its
inputs. Either way, the bundle is not a golden fixture until that is fixed and the
bundle recaptured.

## Handing a bundle over

Bundles hold listening history, marks and known tracks. Archive one with
`tar czf 2026-W39.tgz -C ~/bundles 2026-W39` and move it only to the private
multi-tenant repository.
