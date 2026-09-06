"""`tunefinder publish-pool` — one day's corpus, fetched once and posted to the
multi-tenant API (CONTRACTS §2).

The shape of a run, in order:

  1. mint the run id from the UTC clock, and refuse outright if the generated
     `config/settings.pool.yaml` came from a different taxonomy than the
     vendored one;
  2. read `GET /api/ingest/config` for **every** target before fetching
     anything, so a taxonomy or schema disagreement costs no requests to
     Beatport at all, and so a source the operator switched off is never
     fetched;
  3. take TuneFinder's **existing** data-directory run lock, non-blocking,
     retrying every `pool.lock_retry_seconds` for up to
     `pool.lock_wait_max_seconds` — then skip the day with an alert. The lock
     is held for the fetch and released before any POST: it exists to keep the
     fetchers' token caches and TuneFinder's JSON stores mutually exclusive
     with the Sunday run, and posting touches neither;
  4. build the contract items, the artist payloads and the snapshot, and
     validate every payload against the vendored schemas — a failure here is a
     bug in the publisher and raises rather than posting;
  5. post per target: batches 1..N in order, then the artist payloads, then the
     manifest. Dev and prod acknowledge independently, so a target that fails
     records its error and the next target still runs.

What this module deliberately does **not** do: it never calls
`save_source_items`, `archive_source_items` or `append_run_health`, never posts
a Discord report, and writes nothing outside `data/pool/`. The publisher is a
second consumer of the fetchers, not a second Sunday run.

Secrecy: no log line, snapshot field or alert carries a token, a client secret,
a payload or a url. Alerts pass through `summarise_error`, the same filter the
public status page's `error` strings pass through.
"""
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from src.config import Settings
from src.fetchers import fetch_all_sources
from src.logger import get_logger
from src.pipeline.storage import RunLockHeldError, run_lock
from src.publisher import PUBLISHER_VERSION
from src.publisher.artists import (
    artists_payloads,
    load_artist_index,
    prune_artist_index,
    save_artist_index,
    update_artist_index,
)
from src.publisher.client import PoolApiClient, PoolApiError, TokenProvider
from src.publisher.contract import ContractError, check_item_relations, validate
from src.publisher.identity import IDENTITY_VERSION
from src.publisher.payload import (
    KNOWN_SOURCES,
    SCHEMA_VERSION,
    batch_payload,
    batches,
    build_items,
    manifest_payload,
    per_source_report,
    summarise_error,
)
from src.publisher.snapshots import (
    append_health,
    load_snapshot,
    pool_dir,
    prune_snapshots,
    write_snapshot,
)
from src.publisher.taxonomy import load_taxonomy

logger = get_logger(__name__)

LOG = "[publish-pool]"

# The suffix `mint_run_id` adds: 3 bytes of hex is 6 characters of [a-z0-9],
# inside the contract's 1..16.
_RUN_ID_SUFFIX_BYTES = 3
_RUN_ID_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class PublishOptions:
    envs: list[str]
    dry_run: bool = False
    replay_run_id: str | None = None


@dataclass
class TargetOutcome:
    """What one base URL acknowledged. Dev and prod are independent."""

    env: str
    base_url: str
    batches_acked: int = 0
    upserted: int = 0
    updated: int = 0
    unchanged: int = 0
    obsolete: int = 0
    rejected: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    request_charge: float = 0.0
    artists_posted: int = 0
    manifest: dict | None = None
    latest_advanced: bool = False
    error: str | None = None
    post_seconds: float = 0.0


@dataclass
class PublishOutcome:
    run_id: str
    started_at: str
    skipped: bool = False
    skip_reason: str | None = None
    items: int = 0
    batches: int = 0
    skipped_items: dict[str, int] = field(default_factory=dict)
    fetch_seconds: float = 0.0
    per_source: dict = field(default_factory=dict)
    artist_payloads: int = 0
    targets: list[TargetOutcome] = field(default_factory=list)
    snapshot_path: str | None = None
    total_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.skipped and all(target.error is None for target in self.targets)

    def summary_line(self) -> str:
        """The one line the CLI prints and the release record quotes."""
        head = f"publish-pool {self.run_id} — "
        if self.skipped:
            return head + f"skipped: {self.skip_reason}"

        parts = [f"{self.items} items in {self.batches} batches"]
        for target in self.targets:
            per_item = target.request_charge / max(1, self.items)
            segment = (
                f"{target.env}: upserted {target.upserted} updated {target.updated} "
                f"unchanged {target.unchanged} obsolete {target.obsolete} "
                f"rejected {target.rejected}, RU {target.request_charge:.1f} "
                f"({per_item:.2f}/item), post {target.post_seconds:.1f}s, "
                f"artists {target.artists_posted}/{self.artist_payloads}"
            )
            if target.error:
                segment += f", error: {target.error}"
            parts.append(segment)
        parts.append(f"fetch {self.fetch_seconds:.1f}s")
        parts.append(f"total {self.total_seconds:.1f}s")
        return head + "; ".join(parts)


# ---------------------------------------------------------------------------
# Run ids and the lock
# ---------------------------------------------------------------------------

def mint_run_id(now: datetime) -> str:
    """`<started_at UTC to the second>-<suffix>` (contract "Run ids and ordering").

    The suffix is a tie-break for two runs that start in the same second, not a
    secret — but `secrets` is the right generator for something that must not
    collide across processes.
    """
    return now.strftime(_RUN_ID_FORMAT) + "-" + secrets.token_hex(_RUN_ID_SUFFIX_BYTES)


@contextmanager
def acquire_lock_with_retry(
    data_dir: str,
    retry_seconds: float,
    max_wait_seconds: float,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """TuneFinder's `run_lock`, taken non-blocking and retried.

    Retries every `retry_seconds` while less than `max_wait_seconds` has
    elapsed, then raises `RunLockHeldError` naming the wait — the caller turns
    that into a skipped day rather than an exception, because a Sunday run that
    overran is not an error.
    """
    started = clock()
    while True:
        lock = run_lock(data_dir)
        try:
            lock.__enter__()
        except RunLockHeldError:
            waited = clock() - started
            if waited >= max_wait_seconds:
                raise RunLockHeldError(
                    f"another TuneFinder run held the lock for {waited / 60:.0f} min "
                    f"(waited up to {max_wait_seconds / 60:.0f} min)"
                ) from None
            logger.info(
                "%s run lock held — retrying in %ss (waited %.0fs)",
                LOG, retry_seconds, waited,
            )
            sleep(retry_seconds)
            continue
        break

    try:
        yield
    finally:
        lock.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------

def default_client_factory(settings, env: str) -> PoolApiClient:
    return PoolApiClient(
        settings.pool_api_url(env),
        TokenProvider(
            settings.pool_token_url,
            settings.pool_client_id,
            settings.pool_client_secret,
            settings.pool_scope,
        ),
    )


def _discord_alert(settings):
    def post(message: str) -> None:
        from src.output.discord import make_discord_client

        make_discord_client(settings).post_alert(message)

    return post


def _alerter(settings, alert, dry_run: bool):
    """One place that decides an alert's text and whether it is sent.

    A dry run never alerts. Everything else goes through `summarise_error`, so
    a `requests` message carrying the full request url arrives as `<url>`.
    """
    poster = alert if alert is not None else _discord_alert(settings)

    def raise_alert(message: str) -> None:
        text = summarise_error(message) or message
        if dry_run:
            logger.info("%s dry run — alert not sent: %s", LOG, text)
            return
        logger.warning("%s alert: %s", LOG, text)
        try:
            poster(text)
        except Exception as exc:  # an undelivered alert must not lose the run
            logger.warning("%s alert not delivered: %s", LOG, exc)

    return raise_alert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_RUN_ID_FORMAT)


def _has_pool_credentials(settings, envs: list[str]) -> bool:
    values = [
        settings.pool_tenant,
        settings.pool_client_id,
        settings.pool_client_secret,
        settings.pool_scope,
    ]
    values += [settings.pool_api_url(env) for env in envs]
    return all(values)


def _fetch_settings(settings, switches: dict[str, bool]) -> Settings:
    """The pool settings with every `fetch=false` source disabled.

    A shallow-copied overlay: `settings._data` is the publisher's own tree and
    is never mutated, because the caller may still read it (and because a
    mutated `sources` block would outlive the run).
    """
    data = settings._data
    sources = {}
    for name, config in (data.get("sources") or {}).items():
        sources[name] = config if switches.get(name, True) else {**config, "enabled": False}
    return Settings({**data, "sources": sources})


def _health_for(health: dict, switches: dict[str, bool]) -> dict:
    """One target's view of the fetch: a source it switched off reports as
    disabled even when another target's switch had it fetched."""
    return {name: entry for name, entry in health.items() if switches.get(name, True)}


def _counted(pairs) -> dict[str, int]:
    """Skipped items tallied by reason, busiest first — the table the founder reads."""
    counts: dict[str, int] = {}
    for _, reason in pairs:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _target_record(target: TargetOutcome) -> dict:
    """A target's acknowledgements as the snapshot stores them. No base url:
    the snapshot is a record of what happened, not of where."""
    return {
        "env": target.env,
        "batches_acked": target.batches_acked,
        "upserted": target.upserted,
        "updated": target.updated,
        "unchanged": target.unchanged,
        "obsolete": target.obsolete,
        "rejected": target.rejected,
        "rejected_reasons": dict(target.rejected_reasons),
        "request_charge": target.request_charge,
        "artists_posted": target.artists_posted,
        "complete": bool(target.manifest and target.manifest.get("complete")),
        "latest_advanced": target.latest_advanced,
        "error": target.error,
    }


def _absorb_batch(target: TargetOutcome, body: dict) -> None:
    target.batches_acked += 1
    target.upserted += int(body.get("upserted") or 0)
    target.updated += int(body.get("updated") or 0)
    target.unchanged += int(body.get("unchanged") or 0)
    target.obsolete += int(body.get("obsolete") or 0)
    target.request_charge += float(body.get("request_charge") or 0.0)
    for entry in body.get("rejected") or []:
        reason = entry.get("reason") or "unknown"
        target.rejected += 1
        target.rejected_reasons[reason] = target.rejected_reasons.get(reason, 0) + 1
        logger.warning(
            "%s %s rejected %s %s %s",
            LOG, target.env, entry.get("key_v2"), entry.get("family"), reason,
        )


def _manifest_error(exc: PoolApiError) -> str:
    if exc.status == 409 and exc.error == "batches_missing":
        return (
            f"manifest refused: batches_missing {exc.missing_batches} — "
            "the snapshot holds the acknowledgements; complete it with --replay"
        )
    return f"manifest refused: {exc}"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def publish_pool(
    settings,
    options: PublishOptions,
    *,
    clock=None,
    sleep=time.sleep,
    fetch=fetch_all_sources,
    client_factory=None,
    alert=None,
    taxonomy=None,
) -> PublishOutcome:
    clock = clock or _utc_now
    client_factory = client_factory or default_client_factory
    raise_alert = _alerter(settings, alert, options.dry_run)

    # The wall-clock start of this process. On a replay the run's own
    # `started_at` comes from the snapshot instead, and the two differ.
    run_started = clock()
    run_id = mint_run_id(run_started)
    taxonomy = taxonomy or load_taxonomy()

    if settings.pool_taxonomy_version != taxonomy.version:
        raise ValueError(
            f"config/settings.pool.yaml was generated from taxonomy "
            f"v{settings.pool_taxonomy_version} but the vendored taxonomy is "
            f"v{taxonomy.version} — regenerate it with "
            "`publish-pool --write-settings`"
        )

    outcome = PublishOutcome(run_id=run_id, started_at=_iso(run_started))
    pool_path = pool_dir(settings.data_dir)
    clients: dict[str, object] = {}

    def client_for(env: str):
        if env not in clients:
            clients[env] = client_factory(settings, env)
        return clients[env]

    if options.replay_run_id:
        snapshot = load_snapshot(pool_path, options.replay_run_id)
        run_id = snapshot["run_id"]
        started_at_iso = snapshot["started_at"]
        outcome.run_id = run_id
        outcome.started_at = started_at_iso
        observed_on = date.fromisoformat(started_at_iso[:10])
        built = snapshot["items"]
        batch_size = int(snapshot.get("batch_size") or settings.pool_batch_size)
        per_source = snapshot.get("per_source") or {}
        per_source_by_env = snapshot.get("per_source_by_env") or {}
        outcome.skipped_items = _counted(snapshot.get("skipped") or [])
        outcome.fetch_seconds = float(snapshot.get("fetch_seconds") or 0.0)
        chunks = batches(built, batch_size)
        # The original run already folded these items into the artist index;
        # a replay re-posts the window as it stands, it does not re-count.
        artist_payloads = artists_payloads(
            load_artist_index(pool_path), run_id, taxonomy.version,
            weeks=settings.pool_artist_weeks, today=observed_on,
        )
        logger.info(
            "%s replaying %s — %d items in %d batches", LOG, run_id, len(built), len(chunks)
        )
    else:
        started_at_iso = _iso(run_started)
        observed_on = run_started.date()

        # --- 3. the config gate, before any fetch --------------------------
        fetch_switches_by_env: dict[str, dict[str, bool]] = {}
        batch_size = int(settings.pool_batch_size)
        if options.dry_run and not _has_pool_credentials(settings, options.envs):
            logger.info(
                "%s dry run without pool credentials — not reading /api/ingest/config; "
                "every source switch is on", LOG,
            )
            fetch_switches_by_env = {env: {} for env in options.envs}
        else:
            for env in options.envs:
                try:
                    config = client_for(env).get_config()
                except Exception as exc:
                    logger.error("%s %s config unreachable: %s", LOG, env, exc)
                    raise_alert(f"publish-pool skipped: {env} ingest config unreachable — {exc}")
                    return _skipped(outcome, "config_unreachable", clock, run_started)

                if config.get("taxonomy_version") != taxonomy.version:
                    raise_alert(
                        f"publish-pool skipped: {env} runs taxonomy "
                        f"v{config.get('taxonomy_version')}, the publisher v{taxonomy.version}"
                    )
                    return _skipped(outcome, "taxonomy_version_mismatch", clock, run_started)

                accepted = config.get("schema_versions") or []
                if SCHEMA_VERSION not in accepted:
                    raise_alert(
                        f"publish-pool skipped: {env} no longer accepts schema version "
                        f"{SCHEMA_VERSION} (accepts {accepted})"
                    )
                    return _skipped(outcome, "schema_version_unsupported", clock, run_started)

                batch_size = min(batch_size, int(config.get("batch_size") or batch_size))
                fetch_switches_by_env[env] = {
                    name: bool(entry.get("fetch", True))
                    for name, entry in (config.get("sources") or {}).items()
                }

        # A source is fetched when *any* target wants it; each target's manifest
        # then reports its own switch.
        switch_names = {name for switches in fetch_switches_by_env.values() for name in switches}
        union_switches = {
            name: any(switches.get(name, True) for switches in fetch_switches_by_env.values())
            for name in switch_names
        }
        off = sorted(name for name, on in union_switches.items() if not on)
        if off:
            logger.info("%s sources switched off by the API: %s", LOG, ", ".join(off))

        # --- 4. the fetch, under the lock ----------------------------------
        lock_started = clock()
        try:
            with acquire_lock_with_retry(
                settings.data_dir,
                settings.pool_lock_retry_seconds,
                settings.pool_lock_wait_max_seconds,
                sleep=sleep,
                clock=lambda: clock().timestamp(),
            ):
                logger.info("%s %s fetching under the run lock", LOG, run_id)
                fetch_started = clock()
                raw_items, health = fetch(_fetch_settings(settings, union_switches))
                outcome.fetch_seconds = round((clock() - fetch_started).total_seconds(), 1)
        except RunLockHeldError as exc:
            waited_minutes = round((clock() - lock_started).total_seconds() / 60)
            logger.error("%s %s", LOG, exc)
            raise_alert(f"publish-pool skipped: run lock held for {waited_minutes} min")
            return _skipped(outcome, "lock_held", clock, run_started)

        # --- 5. build -------------------------------------------------------
        corpus = build_items(
            raw_items, taxonomy, observed_on=observed_on, seen_at=started_at_iso
        )
        built = corpus.items
        outcome.skipped_items = _counted(corpus.skipped)
        chunks = batches(built, batch_size)

        configured_enabled = {name for name in KNOWN_SOURCES if settings.source_enabled(name)}
        per_source = per_source_report(
            _health_for(health, union_switches), union_switches, configured_enabled
        )
        per_source_by_env = {
            env: per_source_report(
                _health_for(health, switches), switches, configured_enabled
            )
            for env, switches in fetch_switches_by_env.items()
        }

        index = update_artist_index(load_artist_index(pool_path), built, observed_on)
        index = prune_artist_index(index, settings.pool_artist_weeks, observed_on)
        if not options.dry_run:
            save_artist_index(index, pool_path)
        artist_payloads = artists_payloads(
            index, run_id, taxonomy.version,
            weeks=settings.pool_artist_weeks, today=observed_on,
        )

        snapshot = {
            "run_id": run_id,
            "started_at": started_at_iso,
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": taxonomy.version,
            "identity_version": IDENTITY_VERSION,
            "publisher_version": PUBLISHER_VERSION,
            "batch_size": batch_size,
            "batches": len(chunks),
            "fetch_seconds": outcome.fetch_seconds,
            "items": built,
            "skipped": [list(pair) for pair in corpus.skipped],
            "per_source": per_source,
            "per_source_by_env": per_source_by_env,
            "targets": {},
        }
        outcome.snapshot_path = write_snapshot(pool_path, snapshot)
        append_health(pool_path, run_id, started_at_iso, per_source)
        prune_snapshots(pool_path, settings.pool_snapshot_retention_days, run_started)
        logger.info(
            "%s %s built %d items in %d batches (%d skipped) → %s",
            LOG, run_id, len(built), len(chunks), len(corpus.skipped), outcome.snapshot_path,
        )

    outcome.items = len(built)
    outcome.batches = len(chunks)
    outcome.per_source = per_source
    outcome.artist_payloads = len(artist_payloads)
    if outcome.snapshot_path is None:
        outcome.snapshot_path = write_snapshot(pool_path, snapshot)

    if not built:
        failing = ", ".join(
            f"{name}: {entry['error']}"
            for name, entry in sorted(per_source.items())
            if entry.get("error")
        )
        raise_alert(
            f"publish-pool {run_id} skipped: nothing to publish"
            + (f" — {failing}" if failing else "")
        )
        return _skipped(outcome, "no_items", clock, run_started)

    # Every payload is checked against the vendored schemas before a single
    # POST. A failure here is a publisher bug, not a bad row: bad rows were
    # already dropped with a reason by build_items.
    for batch_no, chunk in enumerate(chunks, 1):
        validate("batch", batch_payload(run_id, batch_no, chunk, taxonomy.version))
        for item in chunk:
            reason = check_item_relations(item)
            if reason is not None:
                raise ContractError(
                    f"built item {item.get('key_v2')!r} would be rejected as {reason}", reason
                )
    for payload in artist_payloads:
        validate("artists", payload)
    for env, report in per_source_by_env.items():
        validate(
            "manifest",
            manifest_payload(run_id, started_at_iso, _iso(clock()), len(chunks), report),
        )

    if options.dry_run:
        outcome.total_seconds = round((clock() - run_started).total_seconds(), 1)
        logger.info("%s dry run — nothing posted. %s", LOG, outcome.summary_line())
        return outcome

    # --- 6. post, per target ----------------------------------------------
    def record_targets() -> None:
        # Updated, not replaced: replaying dev must not erase what prod
        # acknowledged on the original run.
        snapshot.setdefault("targets", {}).update(
            {t.env: _target_record(t) for t in outcome.targets}
        )
        write_snapshot(pool_path, snapshot)

    for env in options.envs:
        target = TargetOutcome(env=env, base_url=settings.pool_api_url(env))
        outcome.targets.append(target)
        client = client_for(env)
        report = per_source_by_env.get(env, per_source)
        post_started = clock()

        try:
            for batch_no, chunk in enumerate(chunks, 1):
                payload = batch_payload(run_id, batch_no, chunk, taxonomy.version)
                try:
                    body = client.post_batch(payload)
                except PoolApiError as exc:
                    target.error = f"batch {batch_no} failed: {exc}"
                    logger.error("%s %s %s", LOG, env, target.error)
                    raise_alert(
                        f"publish-pool {run_id} {env}: batch {batch_no} of {len(chunks)} "
                        f"failed — {exc}; manifest not posted"
                    )
                    break
                _absorb_batch(target, body)
                record_targets()
            else:
                for payload in artist_payloads:
                    try:
                        client.post_artists(payload)
                        target.artists_posted += 1
                    except PoolApiError as exc:
                        # Not fatal: the artist window is a ranking signal, and
                        # the next run re-posts the same thirteen weeks.
                        logger.error(
                            "%s %s artists %s failed: %s", LOG, env, payload["family"], exc
                        )

                manifest = manifest_payload(
                    run_id, started_at_iso, _iso(clock()), len(chunks), report
                )
                validate("manifest", manifest)
                try:
                    body = client.post_manifest(manifest)
                except PoolApiError as exc:
                    target.error = _manifest_error(exc)
                    logger.error("%s %s %s", LOG, env, target.error)
                    raise_alert(f"publish-pool {run_id} {env}: {target.error}")
                else:
                    target.manifest = body
                    target.latest_advanced = bool(body.get("latest_advanced"))
                    if body.get("complete") and not target.latest_advanced:
                        raise_alert(
                            f"publish-pool {run_id} {env}: run complete, but pool "
                            "freshness already belongs to a later run — "
                            "informational, nothing to do"
                        )
        except Exception as exc:  # noqa: BLE001 — an unexpected failure must
            # still alert and let the next target run, not blow up the CLI.
            target.error = summarise_error(f"{type(exc).__name__}: {exc}")
            logger.exception("%s %s post phase failed unexpectedly", LOG, env)
            raise_alert(
                f"publish-pool {run_id} {env}: unexpected error in post phase "
                f"— {target.error}"
            )

        target.post_seconds = round((clock() - post_started).total_seconds(), 1)
        record_targets()

    outcome.total_seconds = round((clock() - run_started).total_seconds(), 1)
    logger.info("%s %s", LOG, outcome.summary_line())
    return outcome


def _skipped(outcome: PublishOutcome, reason: str, clock, run_started) -> PublishOutcome:
    outcome.skipped = True
    outcome.skip_reason = reason
    outcome.total_seconds = round((clock() - run_started).total_seconds(), 1)
    logger.warning("%s %s skipped: %s", LOG, outcome.run_id, reason)
    return outcome
