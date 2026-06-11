# TuneFinder — Scoring System Review

**Date:** 11 June 2026 · **Scope:** `src/pipeline/ranker.py` (weights, signals, section assignment) plus the pool/filter mechanics in `tunefinder/__main__.py` that shape what the ranker sees. All numbers below are computed from the code as of v0.6.6.

**Question asked:** is this the optimal way to score a track, given that the product lives or dies on recommendation quality?

**Verdict up front:** the question is currently unanswerable, and that is the most important finding. There is no outcome data — nothing records whether a recommendation was bought, auditioned, or ignored — so no scoring scheme, including this one, can be called better than another except by argument. The weights are folklore (the discovery report already said this; nothing has changed). The architecture, however, is the right one for this tool: linear, explainable, capped signals with diversity management is exactly what you want at n=1 with a determinism requirement. The chassis is sound. What follows separates (a) structural defects that are wrong at *any* weights and can be fixed now, from (b) tuning questions that must wait for feedback data, and (c) design alternatives worth considering when that data exists.

---

## 1. What the scoring actually does

| Signal | Formula | Effective range |
|---|---|---|
| `known_artist` | `play_count × 3.0` summed over matched artists, capped at 10.0 | 3.0 – 10.0 |
| `recurring_artist` | +2.0 if best `play_count` ≥ 3 | 0 / 2.0 |
| `recent_recommendation` | −0.75 if matched artist recommended in last 4 weeks | 0 / −0.75 |
| `label_match` | 1.5 + 0.5 × min(known artists on label, 3) | 2.0 – 3.0 |
| `cross_source` | 0.5 × min(sources, 4), only when ≥ 2 | 1.0 – 2.0 |
| `genre_match` | 0.5 × matching tags, **uncapped** | 0.5 – ~2.0 |
| `fresh_release` | +0.5 if released ≤ 30 days | 0 / 0.5 |
| `chart_position` | 1.5 × (1 − (pos−1)/100) | ~0 – 1.5 |
| `bandcamp_discovery` | +1.0 flat, every Bandcamp item | 0 / 1.0 |
| `pool_age` | −0.25/week in pool, capped at −1.5 | 0 – −1.5 |

**The score bands this produces** (typical, not theoretical maxima):

- Unknown artist, single source, on-genre, fresh: **~1.0–1.5**
- Same, charting top-10 on Beatport/Mixupload: **~2.5–3.0**
- Unknown artist on a label with 2 known artists: **~3.5–5.0**
- Known artist, `play_count` 1–2 (the majority — 1,322 tracks / 1,175 artists means most profiles hold 1–2 plays): **3.5–7.5**
- Known artist, `play_count` ≥ 4: **12.0** (10.0 cap + 2.0 recurring), minus 0.75 if recently recommended

The artist signal saturates fast: `play_count` 4 already hits the cap, so a 4-play artist and your most-played artist of all time score identically. Below the cap it is a steep ladder (3.0 per play). The practical consequence: **the report's ordering is a familiarity ranking with discovery signals as tiebreakers.** Whenever five or more known-artist tracks survive the filters, Top Picks is all known artists; Artist Watch then takes the *next* known artists by score. Two of the four sections are the same content class, and Wildcards — picked by the same score from the remainder — fills with known-artist overflow and chart material rather than anything wild. Only Label Watch reliably surfaces unknowns.

Whether that is wrong is a product judgment, not a math error: artist-grounded picks are the trust anchor, and explainability is the brand. But a *discovery* tool whose top signal is "you already know this" has a built-in loop, and the section names promise more differentiation than the scoring delivers.

## 2. Structural defects — wrong at any weights, fixable without data

**2.1 The genre signal double-counts popularity and has a noise floor.** Cross-source dedup unions genre tags, so a track seen on three sources accumulates tags *and* earns the cross-source bonus — the same fact (multiple stores carry it) is paid twice. Meanwhile `"electronic"` sits in `_BASELINE_GENRES` and scores 0.5 like any tag, even though the section-assignment code itself exempts it from genre caps *because nearly every track carries it*. A signal that fires on almost everything is a constant, not a signal. Fix: cap `genre_match` at 2 tags and exclude `"electronic"` from the scoring set (keep it for cap-exemption purposes).

**2.2 `fresh_release` is nearly a constant too.** Weekly candidates are already date-filtered to ≤ 28 days, and the freshness threshold is 30 — so every dated candidate gets +0.5 and the signal discriminates almost nothing. The only tracks it separates are pool carryovers, which already pay a pool-age penalty: one fact (age), encoded twice. It also spends report word-budget on "Released N days ago" reasons that carry no information. Fix: drop the signal from scoring (keep `days_old` as a display fact), or set its threshold meaningfully below the window (e.g. ≤ 7 days = genuinely *just* out).

**2.3 The Bandcamp bonus is a source prior wearing a signal costume.** +1.0 flat for being on Bandcamp says nothing about the track. It exists to compensate for Bandcamp's missing chart data — a legitimate goal — but it stacks with Bandcamp's date-filter exemption, so an undated, stale Bandcamp upload competes as if fresh, forever, with a free point. If the goal is "don't let chart-less sources lose structurally", the cleaner mechanism is per-source normalisation or reserved section slots, not a flat additive bonus.

**2.4 The recency penalty cannot do its job.** −0.75 against a 12.0 favourite is a 6% haircut; the same handful of high-`play_count` artists will re-top the report every week they release anything. The per-section artist caps (2) do the real anti-repetition work, but they operate per section — a hot favourite can still appear in Top Picks *and* Artist Watch in one report (caps reset per section by design). If the observed failure mode "same artists every week" still occurs, the fix is multiplicative (e.g. −25% of artist score) rather than a bigger constant — but defer the exact number to feedback data.

**2.5 Weekly and mix-prep runs disagree about pool dates.** `cmd_run` injects pool candidates with no release-date filter; `cmd_mix_prep` applies the window to the pool. One of these is wrong, or the difference is intentional and undocumented. (Pool-as-second-chances arguably *should* ignore the window — in which case mix-prep is over-filtering.)

**2.6 Asymmetric error costs are unmodeled.** Exact-string artist matching has no confidence concept: an alias release scores zero (missed value, invisible), while a name collision produces a false "You play X" reason (visible trust damage — the worst error class this product has). The scoring treats both errors as impossible. The alias map (Phase 4 backlog) fixes the first; the second has no current mitigation at all — worth pulling forward at least a length guard (don't artist-match names below ~4 characters without corroboration).

**2.7 Sections always fill.** Quality at the margin is set by the worst pick in an 18-track report, yet `pick(n)` fills every section to its configured count whenever candidates exist — there is no score floor. A thin week should produce a thin report ("only 9 worth your time"), not a padded one. A per-section minimum score is a 5-line change and probably the single cheapest quality win available before any feedback exists.

## 3. What cannot be fixed without data

Every weight value: 3.0 vs 2.0 for artist plays, the 10.0 cap, label 1.5+0.5n, chart 1.5 linear — none of these can be defended or attacked except with outcome data. The honest statement is that the current weights encode one person's introspection about his own taste structure (artist-loyal, label-aware), which is a reasonable prior and nothing more. Re-tuning them now would be vibes replacing vibes.

This is why the Phase 2 feedback work is not a nice-to-have — it is the prerequisite for this review's question even being answerable. The Phase 1 archiving commit matters here too: once weekly source snapshots exist, you can replay past weeks under modified weights and ask "would this have surfaced what I actually bought?" — backtesting for free, no live runs.

## 4. Alternatives, in the order they become sensible

**Now (with Phase 1 archives + no feedback yet):** the §2 hygiene fixes plus a score floor. All config-driven, all deterministic, each defensible without data.

**After ~2–3 months of feedback (`mark` / Rekordbox library diff):** fit the weights instead of guessing them. A logistic regression over the existing signal vector (the signals are already a clean feature set) trained on bought/ignored outcomes keeps everything the current design values — linear, explainable ("label match contributed 1.2"), deterministic at inference, zero runtime cost — while replacing folklore with evidence. A few hundred labelled outcomes is enough to beat hand-tuning. This is the natural endpoint of the current architecture, not a replacement for it.

**Worth considering at the same time — two-axis scoring:** compute a familiarity score (artist, prior plays, label) and a discovery score (cross-source corroboration, chart, genre fit, *excluding* familiarity) per track. Rank Top Picks by combined, Wildcards by discovery-axis only. This makes Wildcards a deliberate exploration channel instead of a leftovers bin, directly attacks the §1 familiarity-loop problem, and costs nothing — it is a re-read of signals already computed.

**Probably never (at n=1):** embeddings, audio features, collaborative filtering. They break determinism and explainability, cost money or compute, and solve cold-start/scale problems this tool does not have. The discovery report's cross-user ideas died with the SaaS.

## 5. Recommended sequence

1. **Phase 1 ships unchanged** — the spec correctly fences scoring off; none of this blocks it.
2. **Phase 2 feedback capture + replay harness** — unchanged priority; this review strengthens the case.
3. **New "scoring hygiene" item (Phase 2.5):** genre cap + `electronic` exclusion from scoring, fresh-signal removal or re-thresholding, pool date-window consistency decision, score floor per section, short-name match guard. Each shipped with tests, each a one-line config or small function change. No weight re-tuning in this pass.
4. **Evidence-based weights (or fitted linear model) + two-axis Wildcards** once replay shows the feedback data is dense enough to evaluate against.

The system doesn't need to be cleverer. It needs to stop paying twice for popularity, stop scoring constants, and start measuring itself.
