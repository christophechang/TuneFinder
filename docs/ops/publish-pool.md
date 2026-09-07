# Running the pool publisher (`publish-pool`)

`tunefinder publish-pool` fetches the day's releases under the multi-tenant
taxonomy and posts them to TuneFinder's multi-tenant API, which writes them into
the shared candidate pool. It runs daily at 06:00 on the Mac mini, from the same
checkout as the Sunday run.

It is a second consumer of the fetchers, not a second Sunday run.

## 1. What it touches, and what it does not

Writes, all of them under `data/pool/`:

| Path | What it is |
|---|---|
| `data/pool/snapshots/<run_id>.json.gz` | the run's built corpus plus every acknowledgement, rewritten after each batch |
| `data/pool/health.json` | per-source counts and errors, newest 26 runs |
| `data/pool/artist_index.json` | the thirteen-week artist tally the `artists` payloads are cut from |

It never calls `save_source_items`, `archive_source_items` or
`append_run_health`, never writes `data/recommendation_history.json`,
`data/pool.json`, `data/learned_weights.json` or `data/label_affinity.json`, and
never posts a Discord report. The one file it touches outside `data/pool/` is
TuneFinder's existing run lock, `data/.tunefinder.lock` — see §5. The fetchers
write their own token caches (`data/soundcloud_token.json`,
`data/beatport_token.json`) as they always do, under that lock.

It does not call `settings.validate()`: the publisher needs no Discord token to
run. A missing one degrades an alert to a logged warning, exactly as `serve`
degrades report delivery.

## 2. Environment

Six variables in the checkout's `.env`, all optional to every other command:

```
TUNEFINDER_POOL_API_DEV=          # https://tunefinder-api-dev.setfolio.app
TUNEFINDER_POOL_API_PROD=         # https://tunefinder-api.setfolio.app
TUNEFINDER_POOL_TENANT=           # External ID tenant, domain form
TUNEFINDER_POOL_CLIENT_ID=        # the publisher app registration
TUNEFINDER_POOL_CLIENT_SECRET=    # its client secret
TUNEFINDER_POOL_SCOPE=            # api://<API app id>/.default
```

The secret is minted on the mini, straight into this `.env` and nowhere else —
**multi-tenant repo, `docs/ops/HANDOFF-AZURE.md` step 8**. It is never printed,
logged, alerted or written into a snapshot; `check-config` reports each of the
six names as SET or MISSING and never a value.

`TUNEFINDER_POOL_TOKEN_URL` overrides the derived token endpoint
(`https://<first tenant label>.ciamlogin.com/<tenant>/oauth2/v2.0/token`) with
the tenant-GUID form, should the domain form be refused.

```bash
./venv/bin/python -m tunefinder check-config     # SET / MISSING per name
```

After a deploy that changes `requirements.txt`, run
`./venv/bin/pip install -r requirements.txt` on the mini — `publish-pool`
needs `jsonschema` and no other command does, and the deploy runbook does not
pip-install for you.

## 3. The pool settings file

`config/settings.pool.yaml` is **generated, not edited**. It is the whole
multi-tenant taxonomy as a fetch configuration — every Beatport chart, Volumo
genre, Bandcamp tag and SoundCloud target, each row tagged with its fine-genre
id — rendered from `tools/publish-pool-contract/taxonomy.yaml`, plus the
publisher's own `pool:` knobs:

```yaml
pool:
  batch_size: 200                # capped further by the API's own config
  targets: [dev]                 # what `publish-pool` posts to with no --env
  snapshot_retention_days: 14
  artist_weeks: 13
  lock_retry_seconds: 300
  lock_wait_max_seconds: 7200
```

A publish run loads `config/settings.yaml` and replaces its `sources:` block
with the pool file's, then adds `pool:` and `taxonomy_version:`. Everything else
— Discord channels, `data_dir`, scoring — is inherited unchanged, and the Sunday
run never reads the pool file.

Regenerate after a taxonomy change:

```bash
./venv/bin/python -m tunefinder publish-pool --write-settings
# config/settings.pool.yaml — unchanged   (or: — updated)
```

It prints the path and whether the bytes changed, and exits without running.
A test fails if the committed file drifts from what the taxonomy renders, and
the run itself refuses to start (`ValueError`) if the file's `taxonomy_version`
is not the vendored taxonomy's.

## 4. A run, step by step

1. **Mint the run id** — `<started_at UTC to the second>-<6 hex>`, e.g.
   `2026-09-06T06:00:00Z-71bf51`. Runs are totally ordered by `started_at` then
   suffix, and that order decides which run's facts win.
2. **Read `GET /api/ingest/config`**, per target, *before fetching anything*.
   The run is skipped, with an alert, when the API's `taxonomy_version` differs
   from the publisher's (`taxonomy_version_mismatch`), when it no longer accepts
   schema version 1 (`schema_version_unsupported`), or when the call fails
   (`config_unreachable`). The config also carries the `fetch` switch per
   source and the API's own `batch_size` (the smaller of the two wins).
3. **Fetch under the run lock** — see §5. A source with `fetch: false` is
   fetched with `enabled: false`, so it never runs; with two targets a source is
   fetched when *either* wants it, and each target's manifest reports its own
   switch as `enabled`, which is what lets the status page say **disabled**
   rather than **failed**.
4. **Build** the contract items, the thirteen-week artist payloads and the
   snapshot, and **validate every payload** against the vendored JSON Schemas
   before a single POST. A row that cannot be published is dropped with a
   reason and counted in the skipped-items table; a payload that fails the
   schema is a publisher bug and raises.
5. **Post**, per target: batches 1..N in order, then the artist payloads, then
   the manifest. The manifest is the only thing that advances pool freshness,
   and only once every batch is acknowledged. Dev and prod acknowledge
   independently — a target that fails records its error, skips its manifest,
   alerts, and the next target still runs.

## 5. The lock, and the two-hour skip

The publisher takes **TuneFinder's existing** data-directory run lock
(`data/.tunefinder.lock`) — the same one the Sunday run, mix prep and
web-triggered runs already hold. It is taken non-blocking, retried every
`lock_retry_seconds` (300) for up to `lock_wait_max_seconds` (7200), and then
the day is skipped with an alert:

```
publish-pool skipped: run lock held for 120 min
```

That is what makes the Beatport and SoundCloud token refreshes mutually
exclusive with every existing consumer without changing any TuneFinder code
path — no new lock is introduced.

The lock covers **the fetch only**. Posting touches neither the token caches nor
TuneFinder's stores, and a 48-batch upload should not block a web-triggered run
for minutes.

### Forcing the overlap (S9's forced-overlap test)

S9 requires the publisher to be started **while another consumer holds the lock**, in two
cases: a weekly run holding it, and a web-triggered cut holding it. The two never collide
on their own — the publisher fires at 06:00 and the Sunday run at about 09:02 — which is
why the test is a *forced* one, driven through the web service's `POST /api/runs`.

Two properties make this cheap and non-destructive, both worth knowing before you start:

- **`dry_run` still takes the lock, on both sides.** `run_weekly` and `run_mix_prep` enter
  `with run_lock(...)` after their "(DRY RUN)" log line, and the publisher's lock sits in
  step 4, which `--dry-run` does not gate. So the whole test runs with nothing written to
  a target and no Discord report posted.
- **The publisher's first attempt is immediate.** It sleeps `lock_retry_seconds` only
  *after* the first failure, so the yield shows up in the log at once rather than in five
  minutes.

The lock covers the fetch only (about three minutes), so start the publisher within
roughly thirty seconds of the run being accepted. Do not do this near 06:00.

Baselines first, in the checkout:

```bash
shasum -a 256 data/soundcloud_token.json data/beatport_token.json > /tmp/s9-tokens-before.txt
find data/archive -type f | sort | shasum -a 256 > /tmp/s9-archive-before.txt
```

Then, in one terminal, hold the lock — `"mode":"weekly"` for the first case,
`"mode":"mix-prep","genre":"dnb"` for the second:

```bash
curl -s -X POST http://localhost:8420/api/runs \
  -H "Authorization: Bearer $TUNEFINDER_WEB_API_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"mix-prep","genre":"dnb","dry_run":true}'
```

and immediately, in another:

```bash
./venv/bin/python -m tunefinder publish-pool --dry-run 2>&1 | tee /tmp/s9-overlap.log
```

**The pass is this line**, which is the publisher declining to barge in:

```
run lock held — retrying in 300s (waited 0s)
```

Leave it running. When the other run releases, the next retry logs
`<run_id> fetching under the run lock` and the publisher proceeds — S9's "waits" branch,
in about six minutes. To see the "skips" branch instead, lower `lock_wait_max_seconds`
in `config/settings.pool.yaml` temporarily and **restore the file afterwards** (it is
generated, carries a do-not-edit header, and a drift test asserts it matches the
generator); drop `--dry-run` for that one if you want the real Discord alert rather than a
logged one.

Evidence for the S9 record — both diffs must be empty:

```bash
shasum -a 256 data/soundcloud_token.json data/beatport_token.json | diff /tmp/s9-tokens-before.txt -
find data/archive -type f | sort | shasum -a 256 | diff /tmp/s9-archive-before.txt -
```

The token diff is the direct proof of "never refreshes a token while another consumer
runs": the publisher never reached the fetch, so it never touched either token cache.

A caveat worth recording alongside the result: driving the weekly case through
`POST /api/runs` calls the same `run_weekly` and takes the same lock as the scheduled
Sunday trigger, so the lock behaviour is faithful — but it is not the scheduled invocation
itself. Neither case needs the stale `com.openclaw.tune-finder` LaunchAgent fixed.

## 6. `data/pool/` and retention

Snapshots are pruned by the `started_at` in their own file name (never mtime,
which a copy or a restore disturbs) after `snapshot_retention_days` (14). The
health log keeps the newest 26 runs. The artist index keeps `artist_weeks` (13)
weeks of Monday buckets and drops a family that has none left.

Everything under `data/` is gitignored.

## 7. `--dry-run` and `--replay`

```bash
# Fetch, build, validate and snapshot; post nothing. No pool credentials needed.
./venv/bin/python -m tunefinder publish-pool --dry-run

# Re-post a snapshot's batches, artists and manifest under the same run id.
./venv/bin/python -m tunefinder publish-pool --replay 2026-09-06T06:00:00Z-71bf51

# Post to a specific target, or to both.
./venv/bin/python -m tunefinder publish-pool --env prod
./venv/bin/python -m tunefinder publish-pool --env both
```

`--dry-run` still fetches live — that is the point of it — and still writes the
snapshot, the health log and the summary. It posts nothing and **never alerts**.
Without pool credentials it also skips the config call and treats every source
switch as on.

`--replay RUN_ID` is the answer to a run that failed halfway: it reuses the
snapshot's `run_id`, items, `started_at` and per-source report, takes no lock
and does not fetch, and re-posts everything. Re-posting an acknowledged batch is
harmless — the API answers `unchanged` — and the manifest completes the run.
This is what a `409 batches_missing` on the manifest tells you to do; the
missing batch numbers are recorded in the target's error and in the snapshot.

Exit codes: `0` when every target completed, `1` on a skip or any target error.
The summary line, which is what the release record quotes — the dry run of
2026-09-06, which has no target segment because it posted nothing:

```
publish-pool 2026-09-06T20:45:06Z-71bf51 — 9591 items in 48 batches; fetch 457.6s; total 460.5s
```

A live run adds one segment per target, between the batch count and `fetch`:

```
dev: upserted U updated V unchanged W obsolete X rejected Y, RU <total> (<RU/item>), post <s>s, artists <posted>/<families>
```

## 8. Alerts

Posted to the Discord `#alerts` channel, one message per condition, always
filtered through the same summariser the public status page's `error` strings
pass through — so no url, path, token or payload ever reaches a message:

- the lock was held for two hours and the day was skipped;
- the API refused the run at the config gate;
- there was nothing to publish (with the failing sources named);
- a target failed a batch, so its manifest was not posted;
- a target's manifest was refused;
- a target completed but pool freshness already belonged to a later run
  (informational — a late run that did not win the total order).

Alerts are never posted on `--dry-run`.

## 9. The launchd job

A ready-to-edit unit ships in the repo root as
`com.openclaw.tunefinder-publisher.plist`, alongside the weekly-run
`com.openclaw.tune-finder.plist` and the web service's
`com.openclaw.tunefinder-web.plist`. Replace every `YOUR_ADMIN_USER` with the
macOS username and confirm the paths match the checkout.

```bash
nano com.openclaw.tunefinder-publisher.plist

cp com.openclaw.tunefinder-publisher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.tunefinder-publisher.plist

# Verify
launchctl list | grep tunefinder-publisher

# Test trigger
launchctl start com.openclaw.tunefinder-publisher
```

Runs daily at 06:00 local. Logs to `logs/publisher.launchd.log`.

**To disable publishing:**

```bash
launchctl unload ~/Library/LaunchAgents/com.openclaw.tunefinder-publisher.plist
```

That changes nothing about `com.openclaw.tune-finder` — the Sunday run and its
Discord report carry on exactly as before. The publisher is a separate job with
a separate label, and unloading it is the whole of turning it off.

## 10. The vendored contract

`tools/publish-pool-contract/` is copied unchanged from the multi-tenant repo —
it is not a fork. Source:

| | |
|---|---|
| Repository | `christophechang/tunefinder-multi-tenant` |
| Path | `tools/publish-pool-contract` |
| Commit | `dfbe71c` (`main`, 2026-09-06) |

If the two copies drift, the run that discovers it is a morning's publishing
lost, so a change there is a change here in the same week. The plan this
publisher was built from:
<https://github.com/christophechang/tunefinder-multi-tenant/blob/main/docs/superpowers/plans/2026-09-06-m1d-publisher.md>

## 11. What a run costs

Measured on the first dev run from the mini (spike S3, 2026-09-06, run `2026-09-06T21:42:14Z-b38c55`;
the full record is `docs/spikes/S3-publish-path.md` in the multi-tenant repository):

| | |
|---|---|
| fetch (under the run lock) | 187.5 s — Beatport 2,900, Volumo 6,967, Bandcamp 340, SoundCloud 177 rows |
| corpus | 9,591 items in 48 batches, 0 skipped |
| post | 217.3 s, ~4.3 s per batch; 8 artist payloads; manifest |
| total | 417.6 s |
| request units | 78,851 RU, **8.22 RU per item** — 0.23 % of a day at dev's 400 RU/s |
| refused | 165 of 9,666 copies (1.7 %) `throttled` — Cosmos 429s past the API's retry window |
| replay of the same run | 32.0 s, 11,253 RU (1.17 RU/item): `unchanged 9501`, `upserted 165` (the throttled ones), `rejected 0`; the manifest completed without moving freshness |

So a day costs about 80 k RU and seven minutes; the budget is not the constraint, the API's write rate is. A
`throttled` copy is not lost: the next day's observation writes it, or `--replay <run_id>` writes it now.
