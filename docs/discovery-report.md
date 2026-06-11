# TuneFinder SaaS — Discovery Report

**Date:** 11 June 2026
**Status:** Discovery only — no architecture or implementation proposed
**Input analysed:** `~/Development/TuneFinder` repository (Python prototype, v0.6.6), its docs/spikes, runtime data files, and run logs; targeted external verification of market/API facts.

Throughout this report, claims are tagged: **[Fact-repo]** observed in the repository, **[Fact-ext]** externally verified on 11 June 2026, **[Inference]** my reading of the evidence, **[Assumption]** unverified and needs testing.

---

## 1. Executive summary

The repository is not an early draft of the SaaS — it is a mature, well-engineered **single-user recommendation delivery tool** (Python, v0.6.6, 42+ tests, spike/design-doc discipline). It validates one slice of the proposed product extremely well: multi-source release monitoring, heuristic taste scoring, and an LLM-written weekly report. It contains **zero catalogue management**: no Rekordbox/Serato/Traktor code, no collection model, no user model. The taste profile is built from published SoundCloud mix tracklists served by a bespoke companion API (`api.changsta.com`). The largest surface of the proposed SaaS — the catalogue platform — is greenfield.

Five findings should shape everything that follows:

1. **Source fragility is the defining operational characteristic of this domain.** Of nine source integrations attempted, five are dead, blocked, or disabled within roughly six months (Juno shut down entirely; Traxsource and Boomkat Cloudflare-blocked; Bleep login-walled; Bandcamp broke for roughly three weeks when its endpoint was deprecated). This is tolerable hobby maintenance at n=1. At SaaS scale it is an unbounded ops liability and a legal exposure. Source strategy is a first-class product decision, not an implementation detail.

2. **The candidate corpus is user-independent — the pipeline should be inverted.** The prototype fetches ~5,900 items per run for one user. New releases are the same for every user; only scoring is personal. A shared, fetch-once corpus with per-user scoring changes the cost model from O(users × sources) to O(sources) + cheap per-user compute. This single observation makes the bootstrap economics viable and should anchor the logical architecture phase.

3. **The stated primary success metric — recommendation quality — is currently unmeasured and unmeasurable.** Nothing records whether a recommendation was clicked, bought, played, or rejected. History files only prevent repeats. Weights are folklore tuned by one person's vibes. Before any platform build, define the quality metric and the feedback capture mechanism; without it the differentiator cannot be demonstrated, improved, or sold.

4. **Track identity is the trust foundation, and the current approach will not survive multi-user.** Identity is regex-normalised `artist||title` string keys. The normaliser deliberately strips remix/version suffixes, which conflates works a DJ considers distinct (owning "Track (Original Mix)" suppresses every future remix of it, and vice versa). The repo's own data shows dirty keys leaking in (`"15. zero t||sonic bionic"`). Recommending a track the user already owns is the fastest way to lose a DJ's trust; identity quality is therefore upstream of recommendation quality.

5. **The cheapest next step is probably not building the SaaS.** The prototype already proves the loop for one user. The riskiest unknowns are market-shaped (does anyone else want this, what do they do with a recommendation, will they connect their library?), not technology-shaped. A small manual cohort — even run on the existing prototype with per-user profiles — would answer more for less than a .NET rewrite would.

The recommendation pipeline's *learnings* (signal design, rotation/diversity logic, pool persistence, explainability, LLM-at-presentation-layer-only, cost discipline) are worth carrying forward. The *code* is research material, as the brief intends.

---

## 2. Domain understanding

What the domain actually looks like, assembled from repo evidence and verified externals.

**DJs buy tracks; they do not stream them.** The workflow is: discover → preview → buy (Beatport/Bandcamp/etc.) → import into DJ software → tag/grid/cue → play. **[Fact-repo]** The prototype's sources are all purchase stores, and its links resolve to store pages. Discovery tooling must respect that the *action* at the end of the funnel is a purchase decision, usually preceded by an audition. The spike doc claims Volumo has no audio previews, but hands-on testing (11 Jun 2026) found auditioning does work — the spike doc and README are stale on this point. **[Fact — user-verified; corrects earlier Fact-repo claim]**

**Release flow is weekly and chart-oriented.** Stores expose genre charts (top-100), new-release feeds, and label pages. A 28-day window is the prototype's working definition of "new". ~5,900 raw items/week across four sources for ten genres gives a sense of corpus scale: tens of thousands of items/month for one genre footprint. **[Fact-repo]**

**Genre taxonomies are non-standard and politically messy.** Every store slices electronic music differently. The prototype maintains a hand-built mapping of 10 internal genres to each source's taxonomy, including documented "loose fits" (Volumo's Bass House stands in for UK bass) and a combined Beatport feed that must be split per-track. Cross-source tag merging produces contradictory tags that need a config-level exclusion patch (`genre_exclusions`). **[Fact-repo]** This is a permanent property of the domain, not a bug to fix once.

**Track identity is chaotic by nature.** The same work appears as Original/Extended/Radio mixes across stores; remixes, VIPs, dubs, edits, and bootlegs are *distinct works* to a DJ; multi-artist credits are formatted differently per store ("A & B", "A x B", "A feat. B"); artist aliases are endemic in electronic music. ISRCs exist (Volumo returns them) but coverage is partial and they distinguish recordings, not the DJ's notion of "do I own this". **[Fact-repo + Inference]**

**Labels are a strong taste proxy.** DJs follow labels the way readers follow imprints. The prototype's Label Watch section and scaled label scoring reflect this. **[Fact-repo]** But its label knowledge is derived per-run from the current candidate set (artist profiles' `associated_labels` is never populated), so the platform has no persistent artist→label graph. **[Fact-repo]**

**The DJ software ecosystem is fragmented and import is a solved-elsewhere problem.** Rekordbox, Serato, and Traktor each use proprietary library formats. Lexicon DJ has built a paid business ($9.99–$19.99/mo, free format conversion) on exactly this pain, supporting all five major DJ apps. **[Fact-ext]** This proves both that library access is technically feasible and that DJs pay for library tooling — and it means TuneFinder should not plan to win on library management mechanics alone.

**Official data access exists but is gated.** Beatport operates a partner API (OAuth2, granted case-by-case via their engineering team). **[Fact-ext]** MusicBrainz/Discogs offer open metadata APIs, though their coverage of just-released club music typically lags stores. **[Assumption — verify lag empirically]** Everything else in the prototype is scraping or reverse-engineered endpoints. **[Fact-repo]**

**The user's context: a DJ's own published mixes are a high-signal but narrow taste source.** 1,322 known tracks and 1,175 artist profiles were derived from published mix tracklists. **[Fact-repo]** A working DJ's *collection* is typically several times larger than what they have publicly mixed, and contains taste signal (purchases never played out) the mix history misses. **[Assumption — check against Christophe's own Rekordbox library]**

---

## 3. Current repository findings

### 3.1 What it is

A Python 3.11 CLI (`tunefinder run` / `mix-prep <genre>`), scheduled weekly via launchd on the Mac mini, deployed by SSH + git pull. Pipeline: fetch mix-history profile → fetch 4 active sources → dedup/merge → filter (known tracks, prior recommendations, release window) → inject persistent pool → score with weighted heuristics → section assignment with diversity caps → two-stage LLM report → post to Discord → persist history/pool. State is JSON files on disk. **[Fact-repo]**

Last run observed (W23, 6 June 2026): 5,893 fetched → 4,929 after dedup → 4,908 after known-track filter → 4,900 after history filter → +200 pool injected → **18 tracks recommended**. A 0.3% surface rate — this is a *filtering* product, and the cost of a bad pick in an 18-track report is high. **[Fact-repo]**

### 3.2 Domain facts learned from the repository

- **Source mortality is rapid and ongoing.** Juno Download: site shut down June 2026, 295 pool entries purged. Bandcamp: `dig_deeper` endpoint deprecated mid-2026; logs show it healthy on 5 May, failing every tag ("bad function") by the 17 May run, and not restored until the 4 June migration to `discover_web` — roughly three weeks dark. Traxsource: Cloudflare human-verification, disabled. Boomkat: Cloudflare-blocked, never enabled. Bleep: login-walled, never enabled. Mixupload: required reverse-engineering of undocumented URL parameters; JS-rendering nearly killed it at birth. Volumo: undocumented but stable REST API found by inspecting the JS bundle. **[Fact-repo]**
- **Metadata richness varies enormously by source.** Volumo returns BPM, key, ISRC, catalogue number, stable IDs; Beatport returns chart position and BPM; Bandcamp returns no release dates at all (the date filter must exempt it); Mixupload's artist attribution is unreliable (uploader vs artist confusion handled in parsing). **[Fact-repo]**
- **Cross-source corroboration is a usable signal.** ~960 duplicates merged per run; the ranker rewards tracks seen on 2+ sources. **[Fact-repo]**
- **Diversity management is essential to perceived quality.** The prototype accreted per-artist caps, per-release caps, per-genre caps, a 4-week artist recency penalty, and a pool age decay — all responses to real failure modes (same artists every week, one genre flooding the report, stale pool entries lingering). These are domain learnings, not incidental code. **[Fact-repo]**
- **LLM output needs defensive handling.** Control-character stripping for malformed JSON, markdown fence stripping, `<think>` block removal, banned-word lists to suppress marketing prose, and a documented incident: the label-synopsis cache was wiped in v0.6.0 because it contained factual hallucinations (wrong cities, wrong artists, wrong genres). **[Fact-repo]**
- **Known-track exclusion and its data are imperfect.** Sample keys include `"15. zero t||sonic bionic"` — tracklist numbering leaked into an artist name upstream. **[Fact-repo]**

### 3.3 Existing implementation details (context, not targets)

Python dataclasses; JSON file persistence (`candidate_pool.json` capped at 500, history files, label cache); LLM cascade Stage 1 Mistral Small → Groq Llama 3.3 → Gemini Flash → OpenRouter free tier, Stage 2 DeepSeek via OpenRouter; Discord bot with separate report/log/alert/mix-prep channels; fixtures-based offline testing; per-source fetch health surfaced in the report footer. **[Fact-repo]**

### 3.4 Ideas worth preserving (concepts, not code)

- **Explainable signals.** Every score contribution is a `RecommendationSignal(code, explanation)` attached to the candidate. Every recommendation can say *why*. This is the right trust foundation and should survive any redesign.
- **Two-stage LLM at the presentation layer only.** Deterministic ranking, LLM for phrasing; cheap batch enrichment + cached label synopses; cascade with free-tier fallbacks. Cost discipline worth keeping.
- **Persistent candidate pool with age decay.** Good tracks not surfaced this week compete next week instead of vanishing.
- **Mix-prep as a distinct, task-oriented mode** with its own history, so gig preparation doesn't deplete the weekly discovery feed. This is the most differentiated product concept in the repo.
- **Source health transparency** to the end user — at SaaS scale this becomes a status surface, and it sets honest expectations about a fragile upstream world.
- **The funnel-stats footer** (fetched → deduped → filtered → recommended) — operational legibility for free.
- **Spike-before-integrate discipline** (the Volumo spike doc is a model of its kind).

### 3.5 Things in the repo that should NOT carry forward

- String-normalised `artist||title` as the identity model (see §11).
- Per-user full refetch of all sources (see §5/§8).
- Hand-maintained genre maps as the only taxonomy mechanism (see §6, A7).
- Config/code drift already visible at n=1: `pipeline.max_candidates` is loaded but never used; `fetch_all_mixes` (with BPM/energy/mood data) is implemented and never called — the mix-level taste features were planned and silently abandoned. **[Fact-repo]** Worth noting because it shows even the prototype outgrew its own config hygiene.

---

## 4. Product observations

**The prototype optimises the last mile of a product whose first mile doesn't exist yet.** The brief's product is catalogue-first (manage → understand → discover). The repo is discover-only, fed by a bespoke personal API. Catalogue ingestion, identity, and management — the foundation of user trust and the data source for personalisation — are entirely unbuilt. Effort estimates should weight accordingly. **[Inference]**

**The product as built is for one persona: an electronic/club-music DJ who buys tracks weekly.** The genre taxonomy (house/DnB/UKG/breaks/techno…), the sources, and the scoring signals all assume this. That is a *strength* if chosen deliberately — it is a focused ICP with money and a real digging pain — and a liability if the SaaS quietly assumes "all DJs and collectors". A wedding DJ, a hip-hop turntablist, and a vinyl jazz collector would each break the taxonomy, the sources, and the signals. **[Inference]**

**The recommendation report is a push product with no return channel.** Discord delivery works for its single user, and Discord is plausibly where this ICP lives. But the report is fire-and-forget: no act-on-it affordances (save, dismiss, bought-it, own-it-already) and no behavioural capture. The product cannot currently distinguish a beloved report from an ignored one. **[Fact-repo]**

**Mix-prep is the standout concept.** "I'm building a house set — show me the best new candidates" is a job-to-be-done with a deadline, which generic discovery feeds lack. It also naturally tolerates resurfacing (separate history) and justifies richer filters (BPM/key) the data already supports. **[Inference]**

**Quality perception in an 18-track report is asymmetric.** One track the user already owns, one wrong-genre pick, or one hallucinated label blurb damages trust more than seventeen good picks build it. The prototype's changelog is a quiet record of exactly these fights (compilation genre leakage fix, genre exclusion patches, synopsis cache wipe). **[Fact-repo + Inference]**

**Competitive position [Fact-ext + Inference]:** Lexicon owns cross-platform library management; Beatport/Bandcamp own in-store discovery; nothing mainstream does *collection-grounded, cross-store, explainable* discovery for DJs. That intersection is the defensible spot — but it requires both halves (catalogue + recommendations) to work, which is exactly the half the prototype hasn't proven.

---

## 5. Architecture observations

Observations only — options and trade-offs belong to the next phase.

- **The pipeline is rebuilt from scratch every run; only file-based state persists between runs.** Profile, sources, scoring, report — all recomputed weekly. At n=1 this is elegant. The implicit assumption that ingestion cost scales per-user is the main thing to dismantle. **[Fact-repo]**
- **Candidate data is inherently shareable; taste data is inherently private.** The clean seam in the domain: corpus (releases, labels, genres, charts) is global; profile, history, pool, and feedback are per-user. The prototype mixes these in one process and one data directory. **[Inference]**
- **The Mac mini currently does everything** — fetch, score, LLM orchestration, Discord posting — on a residential IP, which incidentally is *less* likely to be bot-blocked than datacenter IPs, a real (if uncomfortable) advantage for scraping workloads. It is also a single point of failure with no redundancy story. **[Fact-repo + Inference]**
- **There is no service boundary anywhere.** No API, no auth, no tenancy, no queue; deploy is SSH + git pull; monitoring is a Discord log channel. None of this is criticism of a personal tool — it is a map of distance to SaaS. **[Fact-repo]**
- **LLM usage is architecturally well-placed** (presentation layer, fallback chains, caching) and trivially cheap at current volume: roughly two calls per weekly run plus uncached label synopses. The pattern survives scale; the free-tier quotas behind it do not. **[Fact-repo + Inference]**
- **The companion API dependency cuts both ways.** `api.changsta.com` cleanly separates "tracklist acquisition from SoundCloud" from "recommendation pipeline" — a sensible boundary. But it is a second bespoke system with its own fragility (SoundCloud scraping/AI tracklist extraction), and the SaaS brief makes it neither the only nor the primary catalogue source. Its role needs an explicit decision. **[Fact-repo + Inference]**

---

## 6. Assumptions that should be challenged

**A1. "The taste profile should come from mix history."** Mix history is what the prototype had, not necessarily what taste *is*. Published mixes are curated public output — they under-represent purchases never played out, over-weight crowd-pleasers, and lag current taste by months. The collection (with added-date, play counts where available) is a richer and larger signal. Likely answer: collection = ownership truth and base taste; mix/play history = weighting on top. But that's a design decision, currently made by default.

**A2. "Recommendation quality is the differentiator."** Partially challenge. Every store has recommendations; what stores cannot do is reason across *your whole cross-store collection* and *exclude what you own*. The defensible differentiator may be the catalogue/identity layer, with recommendations as its visible payoff. If identity is weak, no scoring sophistication rescues trust (see finding 4). Implication: invest in the unglamorous half first.

**A3. "More sources is better."** The repo's own history argues the opposite: loose-fit genre mappings inject noise (Bass House ≠ UK bass), every added source adds taxonomy debt and dedup ambiguity, and half of them die. Fewer, higher-fidelity, more legally durable sources may beat breadth. Candidate volume is already 270× the weekly report size.

**A4. "Scraping can underpin a SaaS."** For personal use, scraping is tolerated custom. For a commercial product it is a ToS violation against the same companies whose links you depend on, a re-engineering treadmill (demonstrated), and a single residential IP away from total outage. At minimum, the legal posture per source must be established before launch; the Beatport partner API **[Fact-ext]** is the obvious de-risking move for the most important source.

**A5. "The same track should never be recommended twice."** Hard suppression is a blunt instrument. A track skipped in a crowded week may be exactly right for next month's mix-prep. The weekly/mix-prep history split already concedes this. With feedback capture, suppression can become state ("dismissed" vs "not acted on" vs "saved"), which is both better UX and better training data.

**A6. "LLM-written reasons improve the report."** The reasons are paraphrases of deterministic signals, and the current output shows template fatigue ("X's sixth prior track, currently #N on the chart…" repeated through a report **[Fact-repo, W23 log]**) plus a documented hallucination incident on label synopses. Deterministic explanations with provenance ("because you played Swandive in your March UKG mix") may earn more trust than LLM prose. The LLM's clear win is the report's editorial voice, not its facts.

**A7. "The genre taxonomy can be hand-maintained."** True only while the ICP stays narrow and the operator is the user. Ten genres × five sources is already showing strain (loose fits, split feeds, exclusion patches). Each new user genre-world multiplies it. Either constrain the ICP deliberately (defensible) or plan a taxonomy mapping approach that doesn't require a human per mapping (hard). Choosing neither, implicitly, is the worst option.

**A8. "Weekly is the right cadence; a report is the right shape."** Weekly batch suits release cycles and digging habits **[Assumption]** — but it's untested beyond n=1, and the report shape (long scroll, four sections) is optimised for reading, not acting. Cadence and shape should be validated, not inherited.

**A9. "The Mac mini is an asset."** It is — free compute, residential IP — and also the reason the system has no redundancy, no scale-out, and a hard coupling to one person's house and ISP. Use it deliberately as the bootstrap ingestion host with a documented failure mode, not as load-bearing infrastructure users implicitly depend on.

**A10. "Build the SaaS next."** The prototype has validated the loop for exactly one user whose taste data was lovingly hand-fed through a bespoke API. The expensive unknowns are demand-shaped, not build-shaped. Challenge the instinct to rewrite; consider a validation cohort first (see §14).

---

## 7. Product risks

- **n=1 product-market fit.** Everything — genres, sources, scoring weights, cadence, channel — is tuned to one user. The product may be a category ("collection-aware DJ discovery") or it may be a personal tool that doesn't generalise. Unknown until non-Christophe users touch it.
- **Trust is one bad recommendation away.** Recommending owned tracks (identity failure), wrong-genre picks (taxonomy noise), or fabricated label facts (LLM) each directly attack the product's stated differentiator. The prototype has hit all three failure classes at small scale. **[Fact-repo]**
- **The catalogue ask is heavy.** "Connect/upload your library" is a high-friction, high-trust request to make of a new user, and the value exchange must be immediate. A DJ's collection is professionally sensitive (set identity, unreleased material). Privacy posture must be explicit from day one.
- **Niche market with an incumbent adjacency.** DJs who buy weekly are a narrow segment. Lexicon ($9.99–$19.99/mo) proves willingness to pay for library tooling **[Fact-ext]**, but it also means the "manage my library" wedge is contested; the uncontested wedge is discovery grounded in the library.
- **Preview gap.** A discovery tool where you cannot audition in-flow leaks its value to the stores. Embed/preview rights vary by store (Volumo previews do work, contrary to the repo's spike doc — user-verified 11 Jun 2026). If auditioning stays off-platform, TuneFinder is a link list with good taste — useful, but harder to retain and monetise. **[Fact-repo + Inference]**
- **Free launch without instrumentation teaches nothing.** If the validation launch ships without the quality metric and feedback capture (finding 3), the only learning will be vanity signups.
- **Dependency on goodwill of scraped parties** whose commercial interest (selling tracks) TuneFinder serves, but whose ToS it likely violates. A cease-and-desist against a free tool with users is an abrupt ending. **[Inference; legal review needed]**

## 8. Architecture risks

Flagged for the options phase — not solved here.

- **Per-user fetch multiplication.** Naively multi-tenanting the current pipeline multiplies scraping traffic by user count — operationally, legally, and economically the wrong shape. The shared-corpus inversion (finding 2) is the mitigation; it must be a day-one decision, not a retrofit.
- **Identity debt compounds in a shared corpus.** String-keyed identity at n=1 self-heals (one user re-tunes regexes). In a shared catalogue, every bad merge/split poisons all users, and unwinding canonicalisation later is data surgery. The identity model must precede the catalogue build.
- **Single-host ingestion.** Mac mini offline = platform-wide stale data, silently. Needs at least: health visibility, graceful staleness semantics, and a documented (even if manual) failover story. Residential-IP scraping also concentrates ban risk on personal infrastructure.
- **State and tenancy.** JSON files → some real store with per-user isolation; auth/identity; secrets management beyond a `.env` on a desk. Standard, but it is the bulk of the build and shouldn't be underestimated because the prototype looks small.
- **Free-tier LLM quotas don't scale.** The cascade gracefully degrades, but N users × weekly reports on consumer free tiers will hit walls unpredictably (rate limits, model retirements, ToS on automated use). Budget per-report cost honestly even if tiny.
- **B1 App Service constraints.** 1 core / 1.75 GB shared across whatever lands on it. Fine for a thin API/UI at validation scale; not fine if scoring, ingestion, or LLM orchestration creep onto it. The boundary between "Azure-hosted" and "mini-hosted" responsibilities is a primary design axis for the options paper.
- **Two bespoke upstreams** (the platform itself and `api.changsta.com`-style tracklist extraction) each with scraping fragility. Counting the four store fetchers, the platform's correctness depends on ~6 reverse-engineered external surfaces. Each needs an owner, health checks, and a kill switch.

## 9. Key open questions

**Product**
1. Who is user #2? (Specific DJ persona, genre footprint, store habits, current digging workflow.) Everything downstream bends to this answer.
2. What does a user *do* with a recommendation? Buy now, wishlist, audition later, push to a Rekordbox playlist? The action defines the delivery surface better than channel debates do.
3. Is collection import a prerequisite for value, or can a degraded mode (artists-you-name, mixes-you-link) deliver a first taste profile with near-zero friction?
4. What's the privacy promise for collection data, stated in one sentence a DJ believes?

**Recommendation system**
5. What is the recommendation-quality metric, concretely? (e.g. % of surfaced tracks receiving a positive action within 14 days; counterfactual "did they later buy what we surfaced".)
6. What feedback events are captured at v1, and where in the UX do they live without becoming homework?
7. How do recommendations improve per-user over time — weight adaptation, embedding similarity, eventually cross-user collaborative signals? (The heuristic engine is the cold-start bridge; what's the second act?)
8. How are artist aliases and collaborations resolved? (MusicBrainz dependency? Manual aliasing? Ignore initially and accept missed signal?)

**Catalogue & identity**
9. What is canonical track identity: composite of store IDs + ISRC + fuzzy match with confidence scores? Where do humans adjudicate ambiguous merges?
10. Are remixes/edits/VIPs distinct identities with a shared "work" parent? (The DJ answer is yes; the current code says no.)
11. Which DJ software is first, and via which mechanism (Rekordbox XML export vs local db read)? What metadata is reliably extractable (play counts? cue data? added dates?)?
12. How is the catalogue kept fresh — manual re-export, watched folder, local agent? (Each step up in freshness is a step up in friction/complexity.)

**Platform & sources**
13. Which sources are load-bearing enough to pursue official access for (Beatport partner API first **[Fact-ext]**), and what's the posture when a scraped source objects?
14. What exactly runs on the Mac mini vs Azure, and what is the contract between them when the mini is dark for two weeks?
15. What does multi-tenancy mean for the corpus — strictly shared candidates with per-user overlays, or any per-user source configuration (which reopens the per-user fetch problem)?

**Commercial**
16. What evidence from the free launch would justify investing in monetisation — and what evidence would justify stopping?

---

## 10. Recommendation quality risks

- **No ground truth, no learning loop.** Outcomes are not captured, so quality cannot be measured, compared across changes, or improved systematically. Every ranker change to date was evaluated by one person reading one report. This is the single biggest gap between "has recommendations" and "recommendation quality is the differentiator".
- **Hand-tuned global weights.** The linear signal weights are folklore. They encode Christophe's taste structure (artist-loyalty-heavy, label-aware) and will not transfer to a user whose taste is, say, label-led or genre-exploratory. Per-user adaptation has no mechanism.
- **Availability and popularity bias.** The corpus is what charts on four stores. Self-released, promo-only, white-label, and slow-burn material is structurally under-represented; chart position is itself a scoring signal, doubling down on popularity. The "Wildcards" section is a patch, not a counterweight.
- **Artist-name matching misses the alias graph.** Electronic artists routinely release under multiple names. A high-signal alias release scores zero. Conversely, name collisions between distinct artists (common with short names) can produce false "you play this artist" claims — a trust-damaging error class the current exact-string match cannot see.
- **Genre tag pollution propagates into scoring.** Cross-source merges produce contradictory tags (patched per-pair in config); compilation tracks inherited album genres until v0.6.6. Genre match contributes to score, so taxonomy noise is quality noise. **[Fact-repo]**
- **Known-track exclusion fails in both directions.** Version-stripping conflation suppresses legitimately new remixes of owned tracks (silent missed value); metadata variance lets owned tracks through as recommendations (visible trust damage). Both stem from the identity model.
- **LLM reasons can overclaim.** Documented synopsis hallucinations; current reasons already show factual awkwardness ("first prior track") and stylistic monoculture. Reasons that misstate the user's own history are worse than no reasons. **[Fact-repo]**
- **Cold start for every new user.** The current profile requires a published-mix API. Without artist/label signals the entire scoring model collapses to genre + freshness + charts — i.e. the same chart feed the user already gets from the store. A deliberate degraded-mode design is needed, or early users will see exactly the generic output that proves the product unnecessary.
- **Quality at the margin is set by the worst pick, not the average** (18-track report, see §4). Scoring should arguably optimise precision at small k with abstention ("only 9 worth your time this week") rather than always filling sections — the current section-count config always fills if candidates exist. **[Fact-repo]**

## 11. Catalogue and metadata risks

- **Identity model.** `lower(artist)||lower(title)` with regex version-stripping conflates distinct works (remix ≠ original; VIP ≠ dub), splits identical works (store formatting differences that survive normalisation), and has no concept of confidence, provenance, or correction. It also already ingests dirty keys from upstream (`"15. zero t||…"`). A SaaS catalogue needs: stable internal IDs, per-source external IDs, ISRC where present, fuzzy matching with confidence, human-visible merge provenance, and an undo path. (Design later; the risk is building any catalogue before this exists.)
- **DJ library data is messier than store data.** User-edited tags, filename-derived metadata, inconsistent remix notation, half/double BPM (87 vs 174), key notation variants (Camelot vs musical), duplicate files (MP3 + WAV + edit), and "Track 01" mysteries. Import quality determines whether the user's first impression of the platform's view of *their own collection* is "it gets me" or "it's wrong about my own library".
- **Open metadata sources lag club music.** MusicBrainz/Discogs coverage of a Beatport-first release is often absent for weeks **[Assumption — verify]**, so canonical enrichment can't rely on them for *new* releases; they help for back-catalogue.
- **Label names are not identities.** "Hospital Records" vs "Hospital"; sub-labels; white labels. Label-based scoring and Label Watch inherit this fuzziness; the prototype keys labels on lowercased display strings. **[Fact-repo]**
- **The catalogue is the trust mirror.** Every visualisation, insight, and "you already own this" judgement renders the catalogue back to the user. Metadata quality issues don't just degrade recommendations — they are *visible* in every surface of the product. Conversely, fixing them (dedup reports, metadata enrichment) is standalone user value: quality work here is product, not plumbing.

## 12. Possible product opportunities

- **Mix-prep as the hero feature.** Deadline-driven digging ("Saturday, 2-hour house set") with BPM/key/energy filters the data already supports. No store offers cross-store, collection-aware gig prep. The prototype proves the concept at n=1.
- **Library hygiene as the acquisition hook.** Duplicate detection, metadata enrichment (year/label completion), and a "health report" give immediate value *in exchange for the library upload* — exactly the value exchange A-risk in §7 demands. The taste profile becomes a byproduct of a chore the DJ already wanted done. (Adjacent prior art: Lexicon charges for this category. **[Fact-ext]**)
- **Provenance-grounded explanations as brand.** "Because you played Swandive in your March UKG mix, and this is Sully's first release since" — explanations citing the user's own history are something no store can say and no black-box recommender will. The signal architecture already supports it.
- **Collection intelligence with personality.** Label loyalty, genre drift over time, BPM/key coverage wheels for harmonic mixing, "your house crates stopped at 2023", artist-you-play-but-own-nothing-from gaps. Cheap to compute, screenshot-able, organically shareable — i.e. free distribution.
- **Watchlists as retention.** Artist Watch/Label Watch already exist as report sections; as subscribable per-entity watchlists with digests they become a habit loop independent of the weekly report's hit rate.
- **Cross-user taste graph (later).** With even hundreds of users, "DJs whose collections overlap yours also dig X" becomes available — the natural second-act recommendation engine the heuristics bridge to (see open question 7). This is also the network moat: collections × taste graphs compound; scrapers don't.
- **Two-sided promo channel (much later).** Labels pay to reach exactly-right DJs; DJs get promos that match their taste profile. Heavy lift, real money, only meaningful with an audience — note it, don't architect for it beyond not precluding it.

## 13. Areas requiring further exploration

1. **Track identity & matching** — the moat and the trust foundation. Needs a dedicated spike with a labelled eval set built from Christophe's own library + the prototype's corpus (real ambiguity, free of charge).
2. **DJ software import** — formats (Rekordbox XML/db, Serato crates, Traktor NML), what's reliably extractable, .NET library landscape vs build-from-spec, refresh mechanics. Rekordbox-first is the obvious sequencing given the user base and existing familiarity. **[Assumption — confirm with ICP work]**
3. **Source strategy & legal posture** — per-source ToS review, Beatport partner API application (long lead time; start early) **[Fact-ext]**, which sources are worth keeping at all under a shared-corpus model, staleness semantics when sources die.
4. **Quality metric & feedback design** — definition, event schema, capture UX, and an offline replay evaluation: the existing history + future purchase data can answer "would the system have surfaced what he actually later bought?" cheaply, before any rewrite.
5. **Taste modelling beyond artist counts** — collection-derived profiles, label affinity with memory (the prototype's per-run label derivation is amnesiac), alias resolution, and whether/when embeddings or audio features enter (cost-gated).
6. **Delivery & action surface** — evaluate Discord bot vs web vs email against the *actions* (audition, save, dismiss, buy, export-to-DJ-software) and feedback capture, not against channel preferences in the abstract.
7. **Validation design for the free launch** — cohort size, onboarding friction budget, instrumentation, success/kill criteria (open question 16).
8. **Mac mini / Azure responsibility split** — workload placement, staleness contract, failure modes, secrets handling. (Belongs in the logical-architecture phase but needs the source-strategy input first.)

## 14. Suggested next-step deliverables

In order — each is small, and each de-risks the next:

1. **Product definition one-pager.** ICP (named persona), jobs-to-be-done ranked (weekly digging / gig prep / library hygiene), the recommendation-quality metric *defined*, competitive positioning vs Lexicon and in-store recommendations, and the validation success/kill criteria. Forces answers to open questions 1–4, 5, 16.
2. **Recommendation evaluation note + offline replay harness design.** How quality will be measured before and after the SaaS exists; feedback event schema; replay evaluation against Christophe's own historical data. Cheap, and it converts "quality is the differentiator" from claim to instrument. (Can be drafted against the existing Python tool without rewriting anything.)
3. **Track identity & matching strategy spike.** Identity model options, confidence/merge/split semantics, alias handling, eval set construction. Output: a written strategy with measured match accuracy on real data — not code.
4. **DJ library import spike (Rekordbox first).** What's extractable, format stability, .NET ecosystem support, refresh mechanics, effort estimate. Output: a written feasibility note.
5. **Source strategy & legal posture review.** Including starting the Beatport partner API conversation, since approval lead time is outside our control. **[Fact-ext]**
6. **Logical architecture options paper** (the agreed next phase) — 2–3 candidate shapes evaluated against: shared-corpus inversion, Mac mini/Azure split, cost classification (free / existing / new recurring), failure modes, and monetisation non-preclusion. Deliverables 1–5 are its inputs; written after them, it can be short and decisive.

---

### External references

- Beatport partner/API access: [Beatport Partner Portal](https://partnerportal.beatport.com/hc/en-us), [API docs root](https://api.beatport.com/v4/docs/), partner OAuth discussion: [music-assistant #4039](https://github.com/orgs/music-assistant/discussions/4039)
- Lexicon DJ (library-management incumbent): [lexicondj.com](https://www.lexicondj.com/), [pricing](https://www.lexicondj.com/pricing)

*Repository evidence cited from `~/Development/TuneFinder` at v0.6.6 (README, CHANGELOG, `config/settings.yaml`, `src/pipeline/*`, `src/fetchers/*`, `src/llm.py`, `data/*.json`, `logs/tunefinder_*.log`, `docs/spikes/*`, `docs/superpowers/*`).*

