# publish-pool contract, v1

What the mini's publisher posts to TuneFinder's API, and what the API answers. Four JSON
Schema files (draft 2020-12), a sample of each, one deliberately invalid sample per
rejection reason, and `taxonomy.yaml` — the genres both sides agree on.

TuneFinder vendors this directory **unchanged**. It is copied, not forked: the publisher
validates its payloads against these files before posting, and the API's tests read the same
files out of this repository. If the two copies drift, the run that discovers it is a
morning's publishing lost, so a change here is a change in both places in the same week.

| File | What it describes |
|---|---|
| `batch.schema.json` | `POST /api/ingest/batch` — up to 200 items of one batch of a run |
| `manifest.schema.json` | `POST /api/ingest/manifest` — a run declaring itself finished |
| `artists.schema.json` | `POST /api/ingest/artists` — one family's weekly artist counts |
| `ingest-config.schema.json` | `GET /api/ingest/config` — what the publisher reads before a run |
| `taxonomy.yaml` | the 8 families and 38 fine genres; the `family` and `fine_genre` enums in `batch.schema.json` are its ids |
| `samples/` | a valid example of each, read by the API's tests |
| `samples/invalid/` | one file per rejection reason, named after the reason it produces (`<reason>.json`, or `<reason>.<variant>.json` where a reason has more than one shape). `bad_observation.future.json` is the odd one out: it is refused by the API's **service**, not by its validator, because the rule is a comparison with the API's clock |

## The live endpoints, and who may reach them

Four routes, on two hostnames:

| Route | Method | What it is |
|---|---|---|
| `/api/ingest/batch` | POST | up to 200 items of one batch of a run |
| `/api/ingest/manifest` | POST | the run declaring itself finished; the only thing that advances pool freshness |
| `/api/ingest/artists` | POST | one family's weekly artist counts |
| `/api/ingest/config` | GET | the taxonomy version, the accepted schema versions, the batch size and the source switches, read at the start of every run |

Dev is `https://tunefinder-api-dev.setfolio.app`, prod is
`https://tunefinder-api.setfolio.app`.

All four take a **client-credentials token carrying the `Pool.Publish` app role**
and nothing else — no cookie, no delegated token, no anonymous request. The
publisher's app registration holds that role; a token with any other role, with
two roles, or with none is refused with 403 before the function runs (CONTRACTS
§3). The publisher is never looked up as an account, because it is not one: it
is a machine in a cupboard, and it reaches no user data at all (ADR 0018).

The status page these routes feed is `GET /api/status`, which is public and takes
no credential.

## The schema is the description; the API's validator is the enforcement

The API does **not** run a JSON Schema library on the request path. It has a hand-written,
typed validator, and that validator is what actually decides. Two reasons:

1. The rules that matter most cannot be written in a schema. `families` must be exactly the
   set of families the item's `fine_genres` belong to; `primary_source` must be one of the
   item's own `sources`; `observation.source` must be one of them too; and no two entries of
   `sources` may name the same source — `uniqueItems` does not catch that, because the two
   entries differ in their `id` and `url`, and draft 2020-12 has no keyword for uniqueness by
   one property. All four are relations between fields, and
   `samples/invalid/family_mismatch.json`, `bad_primary_source.json`, `bad_observation.json`
   and `duplicate_source.json` are valid against `batch.schema.json` and refused by the API.
2. A schema failure is a failure of the whole document. The contract is that one bad item is
   rejected **by name** and the other 199 are written (CONTRACTS §2), which needs per-item
   validation whatever else is true.

The API's `ContractSamplesTests` keeps the two honest: it validates every sample against both
the schema files and the validator, checks that each invalid sample produces exactly the
reason its file name claims, and asserts that the `family`, `fine_genre` and `source_name`
enums in `batch.schema.json` are still the taxonomy's ids and the API's known sources.

## Rejection reasons

Every rejected item comes back as `{key_v2, family, reason}` and every refused request as
`{error, detail, missing_batches?}`. The reason is one of these names, and
`samples/invalid/<reason>.json` is a request that produces it:

`malformed_json`, `unsupported_schema_version`, `bad_run_id`, `bad_batch_no`, `no_items`,
`too_many_items`; `missing_key`, `key_too_long`, `missing_artist_title`, `bad_granularity`,
`unknown_family`, `unknown_fine_genre`, `family_mismatch`, `no_sources`, `unknown_source`,
`duplicate_source`, `bad_primary_source`, `bad_observation`, `bad_date`, `bad_bpm`,
`bad_camelot`, `bad_preview`, `throttled`, `conflict`, `store_error`; `unknown_run`,
`batches_missing`, `bad_batches`; `bad_weeks`, `bad_counts`, `too_many_artists`.

Three of them are worth spelling out, because a publisher can send something a schema
validator accepts and still be refused:

- `missing_key` covers **both** keys. Every item carries `key_v2` (the identity: the pool
  document, the dedup group, the mark and history key) and `key_v1` (matching only). An item
  with one of them blank is refused, because stored without a `key_v1` it could never be
  matched against a library row.
- `duplicate_source` is two entries of `sources` naming the same source. A pool item keeps one
  `{id, url, first_seen, last_seen}` per source; post the richest entry.
- `bad_observation` also covers a `chart_position` below 1. It is a position on a chart or it
  is absent — `null` is the right value for a source that does not rank.
- `bad_observation` also covers an **observation dated later than tomorrow**, UTC. A
  publisher's clock running a little ahead of the API's is not worth refusing an item over,
  so tomorrow is taken; an item observed a year from now is a bug, and would sit in the pool
  long past the 45 days §2 gives it. This one rule is the API service's rather than its
  validator's — it is a comparison with the API's own clock, so nothing offline can make it —
  and `samples/invalid/bad_observation.future.json` is therefore valid against
  `batch.schema.json` *and* accepted by the validator, and refused only by the live endpoint.

`batches_missing` is the only refusal that carries `missing_batches`: the batch numbers of
`1..batches` the API never acknowledged, ascending, and the same gaps `detail` names in words —
so a publisher deciding what to re-post reads numbers rather than a sentence. At most **50** are
listed: `batches` is a number the publisher sends, and a run with more than fifty gaps is one to
re-run rather than back-fill. Every other refusal omits the field entirely rather than sending it
as `null`.

`throttled`, `conflict` and `store_error` are the store's, not the item's: the item was
well-formed and the write did not happen. The publisher may re-post those items in the next
run, or immediately; the pool's merge rules make a repeat harmless either way.

## Run ids and ordering

`run_id` is `<started_at, UTC, ISO 8601 to the second>-<suffix>`, for example
`2026-09-06T06:00:00Z-a3f9c1`, minted by the publisher when the run starts. The suffix is 1
to 16 characters of `[a-z0-9]`.

Runs are **totally ordered by `started_at`, then by the suffix**, and that one order decides
which run's facts win and which manifest may advance freshness. The suffix is only a
tie-break: a run starting at 08:00 with suffix `z` is *earlier* than one starting at 09:00
with suffix `a`. Sorting the whole string as text gives the same answer today; it stops doing
so the moment a suffix changes length, so compare the parts, not the string.

## The pool document's id

A pool document's Cosmos `id` is the **lower-case hex SHA-256 of the UTF-8 bytes of
`key_v2`**, and its partition key is the family. So:

```sh
printf '%s' 'borai, denham audio||make me||rmx:franky rizardo' | shasum -a 256
```

gives the id to look up in Data Explorer when one item needs explaining.

The hash is not a secret and is not used as one. Cosmos forbids `/`, `\`, `?` and `#` in an
id and artist names carry all four, so the key cannot be the id; escaping would be reversible
but every future reader would have to unescape by exactly the same rule. Hashing costs the
ability to read the key off the id, which the document's own `keyV2` field gives back.

## Versioning

`schema_version` is `1`. CONTRACTS §10 gives **one release of overlap**: when version 2
lands, the API accepts 1 and 2 for one release, then drops 1. `GET /api/ingest/config`
reports the versions the API accepts right now, and the publisher reads it at the start of
every run — so a publisher that finds its version missing should stop rather than post.

Fields are **added, never renamed or repurposed**. `additionalProperties` is deliberately not
`false` anywhere in these schemas, and the API ignores fields it does not know: a publisher
one release ahead of the API is a supported state, in both directions.

`taxonomy_version` and `identity_version` travel on every batch and are stamped on every
document written. They are not the same thing as `schema_version`: the wire can stay still
while the genres or the identity rules move, and a press keeps only the copies whose
`taxonomy_version` is the current one.

## Sources

The source names are TuneFinder's fetcher keys, verbatim: `beatport`, `bandcamp`,
`traxsource`, `boomkat`, `bleep`, `resident_advisor`, `mixupload`, `volumo`, `soundcloud`.
Four are in use; the rest are accepted so that turning one on is a switch in the API's
`sources/config` rather than a release.

Each source has three independent switches (CONTRACTS §8), returned by
`GET /api/ingest/config`: `fetch` (the publisher honours it immediately, at the start of the
next run), `display` and `preview` (the API's own, enforced on the next request). A source
the operator has turned off should be reported in the manifest with `enabled: false` and
`count: 0` — that is what lets the status page say **disabled** rather than **failed**.

A source that failed carries the reason in `error`, and that string is shown **verbatim on
the public status page**, which takes no credential — so send a short summary of what went
wrong and no urls, tokens, credentials or file paths. The API truncates anything longer than
**500 characters** and the schema says so, but truncation is a backstop, not a filter: what
is in the first 500 characters is published as written.
