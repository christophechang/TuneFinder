# Reggae & Dancehall Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a ninth family, `reggae-dancehall`, with three fine genres — `reggae`, `dancehall`, `dub` — to the multi-tenant genre taxonomy, so multi-tenant DJs can rank reggae; the single-tenant Sunday run is untouched.

**Architecture:** The taxonomy is data, mirrored in four places and pinned by tests: `tools/publish-pool-contract/taxonomy.yaml` is the authority, `Taxonomy.cs` mirrors it as C# constants, `batch.schema.json` mirrors its ids as enums, and `web/e2e/data.ts` mirrors it as the Playwright fixture. No runtime logic changes in either repo, and **no Python changes in the engine**: the Beatport and Bandcamp fetchers fall through to the configured row name, and the publisher resolves fine genres from `raw_metadata` through `fine_genres_for()`. The taxonomy version stays `1` because the change is purely additive, so every stored pool document keeps its stamp and no tenant's taste moves until they rank the new family.

**Tech Stack:** YAML (taxonomy), C# / .NET 10 + xUnit (API), TypeScript / React / Vitest (SPA), Python 3.11 + pytest (engine publisher), JSON Schema draft 2020-12 (contract).

**Spec:** The ADR 0022 amendment that Task 1 writes is the spec of record — `tunefinder-multi-tenant/docs/adr/0022-two-level-genre-taxonomy.md`. Editing the taxonomy file is a founder decision recorded by amending that ADR; the decisions this plan fixes are listed below and the amendment states them.

## The Beatport mapping — decided on measured volume, founder to ratify in the ADR

Beatport files reggae under **genre 103 "Caribbean"**, whose sub-genres are 283 Reggae, 284 Dancehall, 285 Soca, 286 Dub, 287 Calypso, 288 Ska & Rocksteady. Measured live on 2026-10-23 through the repo's own `_fetch_sub_genre_recent` and the Caribbean top-100, on the same authenticated session a publish run uses:

| Sub-genre | In-window releases (28d) | Rows in the Caribbean top-100 |
|---|---|---|
| 283 Reggae | 100 (hit the per-page cap) | 47 |
| 284 Dancehall | 100 (hit the per-page cap) | 14 |
| 286 Dub | 58 | 2 |
| 285 Soca | 75 | 5 |
| 287 Calypso | 2 | 0 |
| 288 Ska & Rocksteady | 12 | 0 |
| (no sub-genre on the row) | — | 32 |

**Decision: map the whole genre `103 → fine: reggae` as well as the three sub-genres.** The chart measurement settles it. A chart row's own sub-genre wins in `fine_genres_for()`, so 63 of the 100 rows reach the right fine genre by themselves and the genre row only catches the rest. The cost is **5 soca rows in 100** mislabelled `reggae` — soca is not in the taxonomy, so those fall through to the genre row — plus 32 untagged rows taking `reggae`, which the chart's own composition says are mostly reggae and dancehall anyway. That 5% buys the chart-position signal and, more importantly, artist-dictionary coverage: `crawl.py:genre_ids()` reads `beatport.genres` rows only, so a family with no genre row never enters the nightly crawl and its artists never reach the multi-tenant dictionary.

Precedent for an accepted bleed: ADR 0022 decisions C, D and H.

Two things the numbers also settle. The three sub-genre feeds alone would fill the family — each is at or above the pool's `sub_genre_limit: 50` — so this was never a volume decision. And **ska is right to leave out**: 12 releases in 28 days and not one row in the top-100.

The ADR amendment is still the founder's to sign. If it goes the other way, drop the `103` row from Task 1 Step 3, drop Review Focus 5, and record the reason.

## Global Constraints

- **Taxonomy version stays `1`.** No `version:` bump in `taxonomy.yaml`, no change to `Taxonomy.Version`. Precedent: the 2026-09-06 and 2026-10-20 amendments were additive and held version 1.
- **Family id `reggae-dancehall`, display name `Reggae & Dancehall`.** Appended ninth, after `hip-hop-rnb-soul`.
- **Fine genre ids and names, in this file order:** `reggae` / `Reggae`, `dancehall` / `Dancehall`, `dub` / `Dub`.
- **File order is part of the contract.** `TaxonomyTests.FamilyOrder_MatchesTheFile` and `FineGenresOf_EveryFamily_MatchesTheFileOrder` pin it, and the genre screen and the landing page list in that order.
- **`tools/publish-pool-contract/` is vendored unchanged.** The engine's copy is byte-identical to the multi-tenant one; it is copied, not edited in place.
- **No ska, soca or calypso fine genre.** `ska`, `rocksteady`, `soca` and `calypso` stay in `library_no_home`.
- **Volumo contributes nothing.** Verified live 2026-10-23: `GET https://volumo.com/api/v1/genres` returns 35 genres, none reggae, dancehall or dub.
- **Deploy order: multi-tenant API before the engine publishes.** An item naming a family the API does not know is refused per-item as `unknown_family`.
- **Single-tenant is out of scope.** `config/settings.yaml`, `MIX_PREP_GENRES` and the engine's README genre table are not touched.

## Review Focus

1. **A cross-family item.** Cross-source tag merging gives one pool item a reggae fine genre and an existing one (`fine_genres: ["reggae", "breaks-breakbeat"]`); `families` must then be exactly `["breaks-uk-bass", "reggae-dancehall"]` or the item is refused `family_mismatch`. Test in Task 1.
2. **A library tag `dub` on a techno library.** `dub` is leaving `library_no_home` for the `dub` fine genre, while `dub techno` already aliases to `techno-raw-deep`. The whole-field lookup must keep winning, or existing users' imports change meaning. Test in Task 1.
3. **The family before its first publish.** `GET /api/me/genres` must report `poolCount: 0` for `reggae-dancehall` (a counted empty pool), never `null` (unread), and the booth filter must drop a fine genre with `boothCount: 0`. Test in Task 2.
4. **A ninth cell in the rank grid.** `GenreRankGrid` maps `families` dynamically, but the grid was laid out for eight and the comments say so; nine cells may leave an odd last row at either viewport. Asserted count in Task 2, eyeballed in Task 2 Step 8.
5. **Soca arriving tagged `reggae`.** Measured at 5 rows in the Caribbean top-100, plus 32 untagged rows taking the genre row's `reggae`. A decision, not a defect: no test pins it, the ADR amendment states it as accepted bleed and the YAML row carries the comment. Named here so a reviewer does not read it as an oversight.
6. **The ninth family's bit truncated to zero.** Added after the five above, by the Codex verification pass of 2026-10-23, and **the most severe item in this list.** `ArtistDictionary` stores each artist's family mask in a `byte`, so `1 << 8` becomes `0` and a reggae artist silently belongs to no family. Test in Task 1b, which exists only for this.

---

### Task 1: The taxonomy of record

The YAML is the spec and the C# is the implementation, so the count assertions fail against the edited file before the mirror is updated. One task: a reviewer cannot accept an edited file with a stale mirror.

**Repo:** `tunefinder-multi-tenant`

**Files:**
- Modify: `docs/adr/0022-two-level-genre-taxonomy.md` (new amendment section, and the Status line)
- Modify: `tools/publish-pool-contract/taxonomy.yaml`
- Modify: `tools/publish-pool-contract/batch.schema.json:50-110` (the `family` and `fine_genre` enums), **and `:255` `families.maxItems` 8 → 9 and `:264` `fine_genres.maxItems` 38 → 41** — the enum-parity test does not check the maxima, so an item in the new family would validate while a hypothetical ninth-family item alongside eight others would not
- Modify: `src/TuneFinder.Core/Taxonomy/Taxonomy.cs:39-52` (`FamilyEntries`), `:54-92` (`FineGenreEntries`), and the doc comments at `:4`, `:95`, `:123`
- Modify: `src/TuneFinder.Core/Taste/TasteReasons.cs:19,22` (comments)
- Modify: `src/TuneFinder.Core/Ingest/IPoolStore.cs:30`, `src/TuneFinder.Core/Ingest/IArtistsStore.cs:45` (comments)
- Test: `tests/TuneFinder.Core.Tests/TaxonomyTests.cs:52,61`
- Test: `tests/TuneFinder.Core.Tests/Press/CutValidatorTests.cs:175`
- Test: `tests/TuneFinder.Core.Tests/Engine/TaxonomyGenresTests.cs:53`
- Test: `tests/TuneFinder.Api.Tests/` — the ingest validator's `family_mismatch` tests (Review Focus 1)
- Test: `web/libraryMapping.test.ts` (Review Focus 2)
- Modify: `web/e2e/data.ts` — the `aliases` block at `:784` and the `noHome` block at `:998`. `libraryMapping.test.ts` asserts both equal the taxonomy file's `library_aliases` and `library_no_home` verbatim, so the fixture moves in the same edit as the YAML. The `TAXONOMY` constant in the same file is Task 2's

**Interfaces:**
- Consumes: nothing.
- Produces: `Taxonomy.Families` gains `"reggae-dancehall"`; `Taxonomy.FineGenres` gains `"reggae"`, `"dancehall"`, `"dub"`; `Taxonomy.FamilyOrder` has nine entries with the new one last; `Taxonomy.FamilyOf("reggae"|"dancehall"|"dub")` returns `"reggae-dancehall"`; `Taxonomy.FineGenresOf("reggae-dancehall")` returns `["reggae", "dancehall", "dub"]`; `Taxonomy.Version` is unchanged at `1`.

- [ ] **Step 1: Write the failing count assertions**

In `tests/TuneFinder.Core.Tests/TaxonomyTests.cs`:

```csharp
Assert.Equal(9, file.Families.Count);    // Families_MatchTaxonomyYaml, was 8
Assert.Equal(41, file.FineGenres.Count); // FineGenres_MatchTaxonomyYaml, was 38
```

In `tests/TuneFinder.Core.Tests/Press/CutValidatorTests.cs:175`: `Assert.Equal(9, Taxonomy.FamilyOrder.Count);`

In `tests/TuneFinder.Core.Tests/Engine/TaxonomyGenresTests.cs:53`: `Assert.Equal(41, model.Genres.Count);`

- [ ] **Step 2: Run them to verify they fail**

Run: `dotnet test tests/TuneFinder.Core.Tests --filter "FullyQualifiedName~Taxonomy"`
Expected: FAIL — `Families_MatchTaxonomyYaml` reports 8 where 9 was expected.

- [ ] **Step 3: Add the family and its source rows to `taxonomy.yaml`**

Amend the header comment's shape line to `9 families, 41 fine genres` and add a second `amended:` note dated today, ids unchanged, version unchanged. Then:

- `families:` — append `reggae-dancehall: { name: "Reggae & Dancehall" }`
- `fine_genres:` — append `reggae`, `dancehall`, `dub`, each `family: reggae-dancehall`, with the display names from Global Constraints
- `beatport.genres:` — append `{ id: 103, slug: caribbean, fine: reggae }` with a comment naming the accepted soca/calypso bleed (option A; omit this row entirely under option B)
- `beatport.sub_genres:` — append `{ id: 283, slug: reggae, parent: 103, fine: reggae }`, `{ id: 284, slug: dancehall, parent: 103, fine: dancehall }`, `{ id: 286, slug: dub, parent: 103, fine: dub }`
- `bandcamp.tags:` — append `{ tag: reggae, fine: reggae }`, `{ tag: roots-reggae, fine: reggae }`, `{ tag: dancehall, fine: dancehall }`, `{ tag: dub, fine: dub }`
- `soundcloud.targets:` — append `{ fine: reggae, genres: "reggae,roots reggae" }`, `{ fine: dancehall, genres: "dancehall" }`, `{ fine: dub, genres: "dub,dub reggae" }`
- `volumo:` — a comment under `unmapped` recording that Volumo carries no reggae genre at all (verified 2026-10-23), so there is nothing to list
- `library_aliases:` — add `reggae`, `roots`, `roots reggae`, `lovers rock` → `reggae`; `dancehall`, `ragga` → `dancehall`; `dub`, `reggae - dub` → `dub`; `reggae / dancehall` → `reggae`. Each **removed from `library_no_home`** in the same edit. Leave `dubwise → jungle` and its comment alone
- `exclusions:` — unchanged. Reggae contradicts none of the eight

All ids verified live on 2026-10-23: Beatport genre 103 with sub-genres 283/284/285/286/287/288, each sub-genre's 28-day release count and the top-100's composition measured (see the table above); the Bandcamp tags each returned fresh releases through `POST /api/discover/1/discover_web`.

- [ ] **Step 4: Mirror it in `Taxonomy.cs` and the schema enums**

Append `("reggae-dancehall", "Reggae & Dancehall")` to `FamilyEntries` and the three `(Id, Name, "reggae-dancehall")` tuples to `FineGenreEntries`, in file order. Add the one family id to `batch.schema.json`'s `family` enum and the three fine ids to its `fine_genre` enum, each at the end to match file order, **and raise `families.maxItems` to 9 and `fine_genres.maxItems` to 41**. Correct "eight families" to "nine" and "thirty-eight" to "forty-one" in the doc comments listed under **Files**.

- [ ] **Step 5: Run the taxonomy tests to verify they pass**

Run: `dotnet test tests/TuneFinder.Core.Tests --filter "FullyQualifiedName~Taxonomy"`
Expected: PASS, including `FamilyOrder_MatchesTheFile`, `FineGenresOf_EveryFamily_MatchesTheFileOrder`, `FamilyNameOf_EveryFamily_MatchesTheFile`, `FineGenreNameOf_EveryFineGenre_MatchesTheFile` and `LibraryAliases_MatchTaxonomyYaml`.

- [ ] **Step 6: Write the cross-family validator test (Review Focus 1)**

Beside the existing `family_mismatch` tests in `tests/TuneFinder.Api.Tests`, following their naming:

```csharp
[Fact]
public void Validate_ReggaeAndBreaksItem_AcceptsBothFamilies()
// fine_genres ["reggae", "breaks-breakbeat"], families ["breaks-uk-bass", "reggae-dancehall"] -> no reason

[Fact]
public void Validate_ReggaeItemMissingItsFamily_IsFamilyMismatch()
// fine_genres ["reggae", "breaks-breakbeat"], families ["breaks-uk-bass"] -> "family_mismatch"
```

- [ ] **Step 7: Move the fixture's alias and no-home blocks, and write the disambiguation test (Review Focus 2)**

In `web/e2e/data.ts`, add the nine new entries to `aliases` and remove the same tags from `noHome`, exactly as Step 3 changed the YAML — `libraryMapping.test.ts:89` compares the two verbatim. Then, in `web/libraryMapping.test.ts`, beside `Fixture_Taxonomy_IsTheShapeADR0022Fixes`:

```ts
it("Aliases_DubIsReggaeAndDubTechnoIsNot", () => {
  expect(libraryMapping.aliases["dub"]).toBe("dub");
  expect(libraryMapping.aliases["dub techno"]).toBe("techno-raw-deep");
  expect(libraryMapping.noHome).not.toContain("dub");
});
```

- [ ] **Step 8: Run the whole .NET suite and the alias test**

Run: `dotnet test --configuration Release`
Expected: PASS.
Run: `cd web && npx vitest run libraryMapping.test.ts`
Expected: FAIL on the two count assertions at `:128-129` — those belong to Task 2; the alias test passes.

- [ ] **Step 9: Write the ADR 0022 amendment**

A new `## Amendment, 2026-10-23 (reggae)` section stating: the family and its three fine genres; that Volumo has no reggae genre; the Beatport option A bleed as accepted, naming soca and calypso; that ska, rocksteady, soca and calypso stay with no home; the library aliases moved; and that no family or fine genre moves, so the taxonomy version stays `1`. Add the amendment to the Status line, as the four existing amendments are listed.

- [ ] **Step 10: Commit**

```bash
git add docs/adr/0022-two-level-genre-taxonomy.md tools/publish-pool-contract src/TuneFinder.Core tests/TuneFinder.Core.Tests tests/TuneFinder.Api.Tests web/libraryMapping.test.ts web/e2e/data.ts
git commit -m "feat(taxonomy): add reggae-dancehall family with reggae, dancehall and dub"
```

---

### Task 1b: The artist dictionary's family mask

Runs after Task 1 and **before Task 4**. Found by the Codex verification pass of 2026-10-23 and confirmed against the code: a ninth family's bit is `1 << 8` = 256, and the dictionary stores each artist's family mask in a `byte`, so 256 truncates to **0**. Nothing throws. `ArtistDictionary.Create` validates against `AllFamilies`, which is derived from `Taxonomy.FamilyOrder.Count` and so widens to 511 on its own and lets 256 through; the truncation happens afterwards, in a cast the compiler accepts. A reggae artist would then be in the dictionary, never match a reggae family filter, and be reported as belonging to no family at all.

This is the one defect in this change that no test in either repository catches.

**Repo:** `tunefinder-multi-tenant`

**Files:**
- Modify: `src/TuneFinder.Core/Taste/ArtistDictionary.cs:85` (`private readonly byte[] _families`), `:101` (the constructor parameter), `:214` (the `(byte)` cast)
- Test: `tests/TuneFinder.Core.Tests/Taste/` — the existing `ArtistDictionary` tests

**Interfaces:**
- Consumes: Task 1's `Taxonomy.FamilyOrder` with nine entries.
- Produces: an `ArtistDictionary` that stores and returns a ninth family's bit. `MaskOf` already returns `int` and needs no change; `FamilyLists` is already sized `AllFamilies + 1`, so it grows to 512 entries by itself and needs no change either.

- [ ] **Step 1: Write the failing test**

In the existing `ArtistDictionary` test file, following its naming:

```csharp
[Fact]
public void Create_NinthFamilyArtist_KeepsItsFamily()
{
    // One artist whose families are ["reggae-dancehall"] — mask 1 << 8 = 256.
    // Assert the entry comes back with Families == ["reggae-dancehall"], and that
    // a typeahead filtered to that family matches the artist.
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `dotnet test tests/TuneFinder.Core.Tests --filter "FullyQualifiedName~ArtistDictionary"`
Expected: FAIL — the artist comes back with no families, because `(byte)256` stored 0. A failure that reports an empty family list rather than an exception is the confirmation that this defect is silent.

- [ ] **Step 3: Widen the mask storage**

Change `_families` and the constructor parameter from `byte[]` to `int[]`, and drop the `(byte)` cast at `:214`. `int` rather than `ushort` because `MaskOf` and `AllFamilies` are already `int`, so nothing else has to convert.

- [ ] **Step 4: Run the test to verify it passes, then the whole suite**

Run: `dotnet test tests/TuneFinder.Core.Tests --filter "FullyQualifiedName~ArtistDictionary"` then `dotnet test --configuration Release`
Expected: PASS. Watch the dictionary's memory-footprint assertions if any exist — four bytes per artist instead of one.

- [ ] **Step 5: Commit**

```bash
git add src/TuneFinder.Core/Taste/ArtistDictionary.cs tests/TuneFinder.Core.Tests/Taste
git commit -m "fix(dictionary): keep a ninth family's bit in the artist family mask"
```

---

### Task 2: The SPA's fixture, counts and landing page

**Repo:** `tunefinder-multi-tenant`

**Files:**
- Modify: `web/e2e/data.ts:59-61` (comment), and the `TAXONOMY` constant — a ninth block. Its `aliases` and `noHome` blocks were already moved in Task 1
- Modify: `web/e2e/mock.ts:132` (comment)
- Modify: `web/index.html:299-312` (the `section-lede` copy and the `ul.families` list), and `:68` (comment)
- Test: `web/libraryMapping.test.ts:128-129`
- Test: `web/landing.test.ts:105`
- Test: `web/src/taste/genres/GenresPage.test.tsx:167`
- Modify: `web/src/taste/genres/GenreRankGrid.tsx:18,41` and `web/src/taste/genres/GenresPage.tsx:234` (comments)
- Test: `web/src/taste/genres/GenreRankGrid.test.tsx:27` (comment)

**Interfaces:**
- Consumes: Task 1's `taxonomy.yaml` — `web/libraryMapping.test.ts` and `web/landing.test.ts` both read that file directly and compare it with the fixture and the page.
- Produces: `libraryMapping.families` with nine entries and `libraryMapping.fineGenres` with forty-one; `TAXONOMY`'s ninth block as `GET /api/me/genres` serves it in every Playwright run.

- [ ] **Step 1: Update the count assertions and run them to verify they fail**

`web/libraryMapping.test.ts:128-129` → `toHaveLength(9)` and `toHaveLength(41)`; `web/landing.test.ts:105` → `toHaveLength(9)`; `web/src/taste/genres/GenresPage.test.tsx:167` → `toHaveLength(9)`.

Run: `cd web && npm test`
Expected: FAIL — the fixture has eight families and the landing page lists eight.

- [ ] **Step 2: Add the ninth block to `TAXONOMY` in `web/e2e/data.ts`**

`["reggae-dancehall", "Reggae & Dancehall", [["reggae", "Reggae"], ["dancehall", "Dancehall"], ["dub", "Dub"]]]`, appended last, matching the file's order.

- [ ] **Step 3: Add the family to the landing page**

Append `<li data-family="reggae-dancehall">Reggae &amp; Dancehall</li>` to `ul.families`, and change the lede's "eight families" to "nine" and "thirty-eight genres" to "forty-one". `landing.test.ts` asserts the page's ids and names equal the taxonomy file's, in order.

- [ ] **Step 4: Run the web suite to verify it passes**

Run: `cd web && npm test`
Expected: PASS.

- [ ] **Step 5: Write the empty-family test (Review Focus 3)**

In `web/src/taste/genres/GenresPage.test.tsx`, following its naming:

```tsx
it("GenresPage_FamilyWithNothingInThePool_SaysZeroNotUnread", () => {
  // reggae-dancehall at poolCount 0 with countsAvailable true: the cell reads as
  // a counted empty family, not as "not published yet" and not as unread.
});
```

- [ ] **Step 6: Run it to verify it fails, then make it pass**

Run: `cd web && npx vitest run src/taste/genres/GenresPage.test.tsx -t "SaysZeroNotUnread"`
Expected: FAIL first if the three empty states are not already told apart for a family at zero; `GenreRankGrid` already distinguishes them, so a pass here means the behaviour was already right and the test pins it.

- [ ] **Step 7: Correct the "eight families" comments and build**

The four comment sites under **Files**, plus `web/e2e/mock.ts:132`.

Run: `cd web && npm run lint && npm run build && npm run typecheck:e2e`
Expected: PASS.

- [ ] **Step 8: Eyeball the rank grid at nine cells (Review Focus 4)**

Run: `cd web && npm run dev`, open the genre screen at desktop and at 375px, confirm the ninth cell does not leave a broken last row and the rank strip still reads. `npm run visual` is not a gate; re-baseline only if the founder asks.

- [ ] **Step 9: Commit**

```bash
git add web/e2e web/index.html web/libraryMapping.test.ts web/landing.test.ts web/src/taste/genres
git commit -m "feat(web): list the reggae-dancehall family on the genre screen and the landing page"
```

---

### Task 3: API copy, generated types and the repo's prose

**Repo:** `tunefinder-multi-tenant`

**Files:**
- Modify: `src/TuneFinder.Api/Functions/TasteFunctions.cs:23,93,270` — including the `[Operation("The eight families and their fine genres with pool counts, and this user's ranking")]` summary
- Modify: `src/TuneFinder.Api/Functions/FunctionLog.cs:295`, `src/TuneFinder.Api/Booth/PoolSnapshotCache.cs:55` (comments)
- Modify: `src/TuneFinder.Contracts/Taste/GenresResponse.cs:29,46`, `src/TuneFinder.Contracts/ManagePoolResponse.cs:15` (comments)
- Modify: `web/openapi.json` (generated at build time, ADR 0025), `web/src/api/types.gen.ts` (generated), `web/src/api/client.ts:431` (comment)
- Modify: `tests/TuneFinder.Api.Tests/PoolSnapshotCacheTests.cs:640`, `tests/TuneFinder.Infrastructure.IntegrationTests/CutProbeTests.cs:23,51`, `PoolReadProbeTests.cs:99,375`, `TenantCostProbeTests.cs:1226` (comments)
- Modify: `README.md`, `PRODUCT.md`, `DESIGN.md`, `tools/publish-pool-contract/README.md` ("8 families and 38 fine genres"), `CHANGELOG.md` (new entry)

**Interfaces:**
- Consumes: Task 1's `Taxonomy` and Task 2's green web suite.
- Produces: an `openapi.json` and `types.gen.ts` whose operation summary says nine families.

- [ ] **Step 1: Correct the `[Operation]` summary and every comment listed above**

"eight families" → "nine families", "thirty-eight" → "forty-one". Prose only; no behaviour.

- [ ] **Step 2: Regenerate the spec and the types**

Run: `dotnet build --configuration Release` then `cd web && npm run generate-types`
Expected: `web/openapi.json` and `web/src/api/types.gen.ts` change only in the operation summary string.

- [ ] **Step 3: Run both suites**

Run: `dotnet test --configuration Release` and `cd web && npm test && npm run build`
Expected: PASS.

- [ ] **Step 4: Check the two count assertions this plan flagged as possibly unrelated**

`web/src/manage/AccountActions.test.tsx` (seven `toHaveLength(38)`) and `web/src/onboarding/DigDeeper.test.tsx:135,153,360,368` (four `toHaveLength(8)`). If either counts fine genres or families, it is now 41 or 9; if it counts something else (onboarding steps, delete-call fan-out), leave it and say so in the commit body.

- [ ] **Step 5: Write the CHANGELOG entry and commit**

```bash
git add src web README.md PRODUCT.md DESIGN.md tools/publish-pool-contract/README.md CHANGELOG.md tests
git commit -m "docs(taxonomy): nine families in the API copy, the spec and the repo's prose"
```

---

### Task 4: Ship the multi-tenant API

The engine must not publish a reggae item before this lands, or every one of them is refused `unknown_family`.

**Repo:** `tunefinder-multi-tenant`

**Files:**
- Modify: `scripts/smoke-test.sh:794` — the live smoke expects `/api/me/genres` to answer exactly `8` families, in the condition and in both messages. No test suite runs this script, so it fails only when someone runs the smoke against a deployed environment

**Interfaces:**
- Consumes: Tasks 1, 1b, 2 and 3 merged.
- Produces: a dev and prod API that accept the new ids. `GET /api/ingest/config` is what the publisher reads before Task 6.

- [ ] **Step 1: Update the smoke test's family count, then open the PR and merge it the repo's way (ADR 0020, GitHub Flow)**

`scripts/smoke-test.sh:794`: `8` → `9` in the condition and in both the `ok` and `bad` messages.

- [ ] **Step 2: Verify dev accepts the family**

Run: `curl -s https://tunefinder-api-dev.setfolio.app/api/status` and confirm it answers; then, signed in as a test account, `GET /api/me/genres`.
Expected: nine families, the ninth `reggae-dancehall` with its three fine genres, `taxonomyVersion: 1`, and `poolCount: 0` with `countsAvailable: true`.

- [ ] **Step 3: Verify prod the same way, then commit nothing**

---

### Task 5: Vendor the contract into the engine and regenerate

**Repo:** `TuneFinder` (this one), branch `claude/reggae-genre-support-45ed3e`

**Files:**
- Modify: `tools/publish-pool-contract/` — replaced wholesale from the multi-tenant repo
- Modify: `config/settings.pool.yaml` (generated)
- Test: `tests/test_publisher_pool_settings.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the multi-tenant `tools/publish-pool-contract/` as merged in Task 4.
- Produces: `config/settings.pool.yaml` carrying a `beatport.genres` row for `caribbean`, three `beatport.sub_genres` rows, four `bandcamp.tags` entries and three `soundcloud.targets`, each tagged with its fine genre id. No Python source changes.

- [ ] **Step 1: Copy the vendored directory and run the pool-settings test to verify it fails**

```bash
rsync -a --delete /Users/christophechang/Development/tunefinder-multi-tenant/tools/publish-pool-contract/ tools/publish-pool-contract/
git diff --stat tools/publish-pool-contract
./venv/bin/pytest tests/test_publisher_pool_settings.py -v
```

Expected: FAIL — the committed `config/settings.pool.yaml` no longer matches what `generate_pool_settings` renders from the new taxonomy.

- [ ] **Step 2: Regenerate the pool settings**

Run: `./venv/bin/python -m tunefinder publish-pool --write-settings`
Expected: `config/settings.pool.yaml` gains the rows named in **Interfaces** and nothing else; the generated header still reads `v1`.

- [ ] **Step 3: Run the pool-settings test to verify it passes, then the whole suite**

Run: `./venv/bin/pytest tests/test_publisher_pool_settings.py -v` then `./venv/bin/pytest tests/ -v`
Expected: PASS. If any test asserts a taxonomy row count, update it to the new count; no fetcher or publisher source should need a change — `_SLUG_TO_TAGS` and `_tag_to_genre` fall through to the configured name, and `fine_genres_for()` resolves from `raw_metadata`.

- [ ] **Step 4: Confirm the config still loads**

Run: `./venv/bin/python -m tunefinder check-config`
Expected: no new warnings. Missing credentials are reported as usual and are not a failure of this task.

- [ ] **Step 5: Write the CHANGELOG entry and commit**

```bash
git add tools/publish-pool-contract config/settings.pool.yaml tests CHANGELOG.md
git commit -m "feat(publisher): vendor taxonomy with the reggae-dancehall family and regenerate pool settings"
```

---

### Task 6: First publish and verification

**Repo:** `TuneFinder`

**Files:** none — this is a run.

**Interfaces:**
- Consumes: Task 4's deployed API and Task 5's regenerated settings.
- Produces: pool documents in the `reggae-dancehall` partition, and the counts `GET /api/me/genres` then reports.

- [ ] **Step 1: Publish to dev only**

Run `tunefinder publish-pool` with its targets narrowed to `dev`. This fetches live and is the operator's call to make, not a step to run unattended.
Expected: the run's rejection summary names no `unknown_family` and no `unknown_fine_genre`.

- [ ] **Step 2: Confirm the family has items**

Run: `GET /api/me/genres` on dev, signed in as a test account.
Expected: `reggae-dancehall` with a non-zero `poolCount`, and a `boothCount` no greater than it.

- [ ] **Step 3: Spot-check the tags against Review Focus 5**

Read a handful of the new partition's documents and confirm the Beatport ones carry the fine genre their sub-genre implies. Under option A, expect some soca or calypso rows tagged `reggae` from the Caribbean chart: that is the accepted bleed, and finding it is confirmation, not a bug.

- [ ] **Step 4: Publish to prod, then report the family's count to the founder**

---

## Self-review notes

- **Spec coverage.** Every constraint in Global Constraints has a task: version (Task 1 Step 3), ids and order (Task 1 Steps 3–5), vendoring unchanged (Task 5 Step 1), no ska/soca/calypso (Task 1 Step 3), Volumo nothing (Task 1 Step 3 comment), deploy order (Task 4 before Task 6), single-tenant untouched (no task names `config/settings.yaml` or `MIX_PREP_GENRES`).
- **Review Focus coverage.** 1 → Task 1 Step 6. 2 → Task 1 Step 7. 3 → Task 2 Steps 5–6. 4 → Task 2 Steps 1 and 8. 5 → Task 1 Step 9 and Task 6 Step 3, as a decision with no test, said so explicitly. 6 → Task 1b, which exists for it alone.
- **Codex verification pass, 2026-10-23.** `gpt-5.6-sol` at medium reasoning effort, read-only, scoped to four factual claims rather than to the plan. It confirmed the three claims this plan rests on — no Python change in the engine, the dictionary's coupling to `beatport.genres`, and the `dub` alias being safe (it found the extra detail that a bare `Dub` in the Mix field is discarded at `web/src/library/mapping.ts:424`). Its fourth answer produced three findings, all three verified against the code and all three now in the plan: the `byte` family mask (Task 1b), `batch.schema.json`'s `maxItems` (Task 1), and `scripts/smoke-test.sh` (Task 4). The first of those invalidated a claim made while planning — that the build would catch anything that mattered. It would not have caught this.
- **Known soft spot.** Task 3 Step 4 is a check rather than a decision, because the two assertion sites could not be attributed from their names alone without reading tests this plan does not otherwise touch. It is scoped to "read it, then either change the number or say why not".
