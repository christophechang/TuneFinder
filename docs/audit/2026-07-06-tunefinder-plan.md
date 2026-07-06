# TuneFinder — Discovery Improvement Plan

**Date:** 6 July 2026
**Derived from:** `docs/audit/2026-07-06-tunefinder-audit.md` (section references below point there)
**Goal:** make TuneFinder genuinely smarter at working out what Christophe likes and surfacing tracks he'll want — better taste modelling, better signals, better candidate sourcing, a real exploration channel, and a closed feedback loop. Few strong ideas, sequenced so each builds on the last.

**Standing guardrails (all items):** deterministic at run time, explainable per-track (`RecommendationSignal` for every score contribution), config-driven weights over hardcoded constants, tests ship with behaviour, snapshot updates deliberate, no LLM/network in the render path, `data/` treated as precious, disabled sources stay disabled. Weekly report shape and Discord formatting stay stable unless an item says otherwise.

**Live-run note:** the cloud session can build and unit-test everything offline (fixtures + mocks). Items flagged **needs-live-run** additionally require a run at home for real-data validation: LLM cascade doesn't exist any more, so "live" here means catalogue API (`api.changsta.com`), store fetches, archived `data/` state on the Mac mini, or Discord.

---

## Sequencing overview

| # | Item | Effort | Depends on | Needs live run | Model tier |
|---|---|---|---|---|---|
| P1 | Config-driven scoring weights | S | — | no | haiku |
| P2 | Two-axis scoring — Wildcards becomes a real discovery channel | M | P1 | validation only | sonnet |
| P3 | Genre affinity weights in the taste profile | S–M | P1 | validation only | sonnet |
| P4 | Alias map + short-name match guard | S | — | no | sonnet |
| P5 | Label affinity memory (persistent artist→label graph) | M | — | backfill at home | sonnet |
| P6 | Scene one-hop signal (label-mates of your artists) | S–M | P5 | validation only | sonnet |
| P7 | Replay harness + feedback-driven weight report | M–L | P1 | real replay at home | opus |
| P8 | BPM/key-aware mix-prep | M | — | validation only | sonnet |
| P9 | Remix-aware track identity | M–L | — | **yes** (rebuild known_tracks) | opus |
| P10 | Taste-led candidate sourcing (label-roster fetcher) | L | P5 | **yes** (live probing) | opus |
| P11 | Taste recency + negative feedback signal | M | P1, P7 helpful | **yes** (mixes endpoint) | sonnet |
| P12 | Robustness & drift hygiene bundle | S | — | no | haiku |

Recommended build order: P1 → P2 → P3 → P4 → P5 → P6 → P8 → P7 → P12 → P9 → P11 → P10. (P9–P11 back-loaded: highest care / most home-dependent.)

---

## P1 — Config-driven scoring weights *(foundation)*

**Audit ref:** §5, §11 guardrails. **Why:** every subsequent item tunes or extends scoring; the `_W_*` constants in `ranker.py` must become a `scoring:` block in `config/settings.yaml` (with the current values as defaults in `src/config.py`) so replay experiments (P7) and weight changes are config diffs, not code edits.

**Approach:** add `scoring:` section to settings.yaml mirroring every `_W_*` / cap / threshold constant; `Settings` properties with today's values as defaults; `ranker.py` reads from settings (thread `settings` into `_score` or resolve a weights object once per run). Zero behaviour change — existing tests must pass untouched except where they construct settings.
**Effort:** S. **Dependencies:** none. **Live run:** no. **Tier:** haiku (mechanical, fully guarded by existing tests).

## P2 — Two-axis scoring: familiarity vs discovery

**Audit ref:** §1.2, §5, §10.1; design sketched in `docs/scoring-review.md` §4. **Why:** the single score is a familiarity ranking; Wildcards is a leftovers bin. This is the cheapest structural change that makes the report *discover*.

**Approach:** during `_score`, accumulate two sub-totals from the same signals — familiarity (known_artist, recurring_artist, and the recency penalty) and discovery (label_match, cross_source, genre_match, chart, freshness, bandcamp/source prior) — stored on the Candidate (e.g. `familiarity_score` / `discovery_score`; total stays their sum, so Top Picks / Label Watch / Artist Watch ordering is unchanged and snapshots barely move). Change **Wildcards selection only**: rank by discovery score, and require `familiarity_score == 0` (or below a small config threshold) so it surfaces genuinely unknown artists. Add a `discovery_pick` signal/reason line so the report can say why a wildcard is there ("Unknown artist — corroborated by 3 stores, on-genre, charting"). Config: `scoring.wildcards_axis: discovery|combined` for rollback. Update snapshot tests deliberately.
**Effort:** M. **Dependencies:** P1. **Live run:** dry-run at home to eyeball a real week; not blocking. **Tier:** sonnet.

## P3 — Genre affinity weights

**Audit ref:** §4 ("genre preference is binary"), §10.5. **Why:** playing 400 DnB tracks and 3 downtempo tracks currently produces the same +0.5 genre bonus. Genre share is the strongest cheap taste dimension not yet modelled.

**Approach:** at profile build time, compute a corpus-level genre distribution from `Track.genres_seen` weighted by `recurrence_count`; persist as `data/genre_affinity.json` (`{genre: share}`); in the ranker, scale `genre_match` per tag by a config-capped affinity multiplier (e.g. `0.5 × (0.5 + 1.5 × normalised_share)`, so a dominant genre ≈ ×2 and a fringe genre ≈ ×0.5 of today's value; exact shape config-driven via P1). Keep the binary genre *set* for section caps and mix-prep filtering — only the score scales. Reason text unchanged (still "Tagged: …"). Graceful default: missing affinity file → flat multiplier 1.0 (today's behaviour).
**Effort:** S–M. **Dependencies:** P1. **Live run:** `build-profile` at home to generate real affinity data; logic verifiable offline. **Tier:** sonnet.

## P4 — Alias map + short-name match guard

**Audit ref:** §4 ("no aliases"), §10.6; `improvement-plan.md` Phase 4 backlog; `scoring-review.md` §2.6. **Why:** aliases are endemic in electronic music (missed matches, invisible); short-name collisions produce false "You play X" claims — the single worst trust error class, currently unmitigated.

**Approach:** new `config/aliases.yaml` (`canonical: [alias, alias…]`, hand-maintained, empty by default, documented with examples). Merge at profile load: alias names resolve to the canonical profile for matching *and* display. Guard: an artist-name match of < 4 characters (config: `scoring.min_artist_match_len`) only fires `known_artist` when corroborated by a second signal on the same candidate (label_match or genre_match), otherwise it is skipped and logged. Applies everywhere `profiles_lower` is consulted (ranker, reasons, report stats) via one shared resolution helper — not three copies.
**Effort:** S. **Dependencies:** none. **Live run:** no. **Tier:** sonnet.

## P5 — Label affinity memory

**Audit ref:** §4 ("amnesiac label affinity"), §10.2. **Why:** labels are the strongest scene proxy the data has, and today a label only "exists" if one of your artists released there *this week*. Persistent memory makes Label Watch fire on quiet weeks and unblocks P6/P10.

**Approach:** new `data/label_affinity.json` store (module `src/pipeline/labels.py`): `{label_key: {display_name, artists: {artist: last_seen_iso}, first_seen, last_seen}}`. Updated each run from the `label_seed` candidate set (known-artist ↔ label co-occurrences the ranker already computes — persist instead of discard). `_build_relevant_labels` returns the union of per-run derivation (today's behaviour) and the store, with the store's artist counts feeding the same `1.5 + 0.5×n` formula; config cap on how stale a stored association may be (e.g. 26 weeks). One-off backfill command `tunefinder backfill-labels` replays `data/archive/*.json.gz` through the same accumulation to seed the store from history. Reasons/label-watch lines already support artist-name facts — they just get more of them.
**Effort:** M. **Dependencies:** none (P1 for the staleness cap being config). **Live run:** backfill + first real run at home (archives live on the mini); logic + backfill tested offline against synthetic archives. **Tier:** sonnet.

## P6 — Scene one-hop signal (label-mates)

**Audit ref:** §6 ("no scene-adjacency"); `improvement-plan.md` Phase 4 "scene-graph one-hop". **Why:** the first scoring expansion beyond exact artist matching that stays deterministic and explainable: *unknown* artists who share a label with artists you play deserve a nudge — and a reason line that names the connection.

**Approach:** using the P5 store, build `{artist_key: [(label, known_artist)]}` for artists observed on affinity labels. New signal `scene_adjacent` (config weight, suggest +0.75–1.0, capped once per track; never stacks with `known_artist`): "Label-mate of Calibre on Signature." Feeds the discovery axis (P2), making Wildcards markedly better. Guard against mega-labels: only labels below a config roster-size cap contribute (a label with 500 artists is not a scene).
**Effort:** S–M. **Dependencies:** P5 (and P2 for full effect). **Live run:** validation dry-run at home. **Tier:** sonnet.

## P7 — Replay harness + feedback-driven weight report

**Audit ref:** §8, §10.3; `scoring-review.md` §3–4; `improvement-plan.md` Phase 4 "offline replay". **Why:** the loop is half-built: outcomes are captured but nothing learns. Replay + per-signal precision turns every future weight change from vibes into evidence. This is the keystone item.

**Approach:** two commands, both offline, no Discord, no writes to live state:
- `tunefinder replay --week 2026-W23 [--set scoring.w_known_artist=2.0 …]` — load `data/archive/source_items_{week}.json.gz`, run dedup → filters → rank with current-or-overridden config (P1 makes overrides trivial), print the report (or a compact diff vs. what `recommendation_history.json` says was actually recommended that week). History/known filters evaluated *as of now* (documented, same caveat as `explain`).
- `tunefinder tune-report` — join `feedback.json` × history records; per signal code, source, genre: recommended count, marked, positive rate, plus lift vs. baseline; flag signals whose positive rate is significantly below/above their weight share. Pure reporting — no auto-tuning at n=1 until feedback density justifies fitting (explicitly out of scope here; revisit once `stats` coverage is meaningful).
**Effort:** M–L. **Dependencies:** P1. **Live run:** real replay needs the archives + feedback on the mini; ship with synthetic-archive tests. **Tier:** opus.

## P8 — BPM/key-aware mix-prep

**Audit ref:** §5 ("BPM/key captured, displayed, used by no filter"), §10.8; `improvement-plan.md` Phase 3. **Why:** mix-prep is the mode with explicit DJ intent; tempo/key are the facts a DJ actually filters by, and the majority of the corpus (Volumo + Beatport + Mixupload) already carries them.

**Approach:** `mix-prep <genre> --bpm 170-180 [--key 8A]`. BPM filter: numeric range, half/double-time aware (85 matches 170 when `--bpm-flex` on, default on for dnb). Key: normalise store notations (Volumo `keysign`, Mixupload `KEY: Cm`) to Camelot; `--key` keeps exact + adjacent wheel positions (±1, relative major/minor); tracks with unknown BPM/key are *kept but demoted below matches* (never silently dropped — coverage is partial). Show BPM/key in the mix-prep track line and audition page (page already does). Key-normalisation table is a pure function with thorough tests.
**Effort:** M. **Dependencies:** none. **Live run:** validation dry-run at home. **Tier:** sonnet.

## P9 — Remix-aware track identity

**Audit ref:** §10.7; discovery-report §11; `improvement-plan.md` Phase 3 ("highest-care change in the plan"). **Why:** owning "Title (Original Mix)" currently suppresses every future remix of that title; a named remix is a distinct work to a DJ. This is a real discovery leak *and* the most regression-prone area in the codebase (duplicate-recommendation risk).

**Approach:** extend `make_dedup_key` to emit a version qualifier: Original/Extended/Radio/album-version normalise to none (merge, as today); *named* remixes/VIPs/reworks canonicalise the remixer into the key (`artist||title||rmx:<normalised remixer>`). Known-track filtering and history filtering compare full keys; dedup merging groups by full key. Backward compatibility: history files contain old-style keys — `build_history_keys` already stores raw + normalised, extend it so old records still block their exact matches. Requires `build-profile` re-run at home to rebuild `known_tracks.json`. Ship behind config flag `pipeline.remix_aware_identity` default **off**; flip on at home after a dry-run diff shows no owned tracks resurfacing. Exhaustive test matrix (the repo already has strong dedup tests to extend).
**Effort:** M–L. **Dependencies:** none (P7 replay is the ideal validation tool — replay a past week with the flag on and diff). **Live run:** **yes** — known-tracks rebuild + dry-run diff at home before enabling. **Tier:** opus.

## P10 — Taste-led candidate sourcing: label-roster fetcher

**Audit ref:** §6, §10.4. **Why:** every current source answers "what's charting in genre G" — the corpus is popularity-shaped before scoring begins. The highest-precision candidate stream available is "new releases on the labels my catalogue points at", which no chart will surface reliably (and which makes known-artist releases visible even when they don't chart).

**Approach:** new fetcher `src/fetchers/label_watch.py` driven by the top-N labels (config, suggest 25) from the P5 affinity store: fetch each label's releases page on Bandcamp (`{label}.bandcamp.com/music` or label search) and/or Beatport label pages (`/label/{slug}/{id}` — `__NEXT_DATA__`, same technique as the existing chart fetcher). Emit SourceItems with a `label_roster` origin flag; a small config-weighted `label_roster` signal (or reuse `label_match`, which will fire naturally). Respect existing throttling; cap requests (N labels × 1 page). **This is the one item that must start with a live spike** — URL discovery per store label entity (label name → Bandcamp subdomain / Beatport id is not a pure function; store resolved handles in the affinity store, resolved once, curated manually where ambiguous). Follow the repo's spike-doc discipline (`docs/spikes/`), then fixtures, then fetcher.
**Effort:** L (spike + fetcher + tests). **Dependencies:** P5. **Live run:** **yes** — spike + fixture capture + first run all need home network. Cloud session can deliver: spike checklist, fetcher skeleton keyed to fixtures, config plumbing. **Tier:** opus.

## P11 — Taste recency + negative feedback signal

**Audit ref:** §4 ("no taste recency", "no negative signal"), §8. **Why:** the profile is a lifetime average — a 2019 phase weighs as much as last month; and `skip` marks currently teach nothing.

**Approach:** two small, independent signals under one ticket:
- **Recency:** use `fetch_all_mixes()` (finally gains its caller) to timestamp each artist's plays via mix `published_at`; compute a recency-weighted play count (e.g. half-life 18 months, config) stored alongside `play_count` in profiles; `known_artist` scoring uses the weighted value. Fallback: tracks endpoint has no dates → if mixes fetch fails, use unweighted counts (today's behaviour).
- **Negative feedback:** artists with ≥2 latest-mark `skip` outcomes and zero positives get a config-weight soft penalty (`skipped_artist`, suggest −1.0, with a reason line so it's visible/debuggable). Latest-mark-per-key semantics reuse `summarise_feedback`'s logic.
**Effort:** M. **Dependencies:** P1; P7 useful for validation. **Live run:** **yes** — mixes endpoint fetch + real feedback file live at home; offline tests via fixtures. **Tier:** sonnet.

## P12 — Robustness & drift hygiene bundle

**Audit ref:** §2, §5, §10.11. **Why:** small trust/ops leaks around the discovery path; none big enough for its own ticket, together worth one pass.

**Scope (checklist):**
- Degraded profile mode: if `fetch_all_tracks` fails, fall back to `data/artist_profiles.json` + `known_tracks.json` from the previous run with a loud alert, instead of dying pre-report.
- Per-source share cap in section assignment (config, e.g. `pipeline.max_share_per_source: 0.6`) so one store can't be the whole report (audit: Volumo ~72% of corpus).
- Move the hardcoded `_DEFAULT_BASE_URL` personal URL out of `catalog.py` (settings default instead; keep behaviour).
- README/config drift: Bandcamp *does* return release dates now (fix table + filter-exemption comment); Mixupload UKBass URL comment vs `page1` code; plist path note.
- Mixupload `download_count`/`stream_count`: either wire as a tiny capped popularity signal on the discovery axis or delete the parsing — no captured-but-unused data.
**Effort:** S. **Dependencies:** none (share cap lands cleanly after P2). **Live run:** no. **Tier:** haiku.

---

## Explicitly out of scope (and why)

- **LLM re-introduction into the run path** — the v0.7.0 removal rationale stands; any future model-driven help (alias suggestion, genre-map maintenance) would be offline batch jobs producing reviewed data files, and none is needed for the items above.
- **Embeddings / audio analysis / collaborative filtering** — break determinism and explainability, solve scale problems this n=1 tool doesn't have (`scoring-review.md` §4 "probably never").
- **Auto-fitted weights** — premature until feedback density is known; P7 builds the measurement instrument first.
- **Re-enabling Traxsource/Boomkat/Bleep** — blocked upstream; CLAUDE.md requires approval.
- **Pipeline dedup refactor of `cmd_run`/`cmd_mix_prep`/`explain`** — real friction (audit §3) but restructuring for its own sake is against house rules; revisit only if the items above make the duplication actively painful.
