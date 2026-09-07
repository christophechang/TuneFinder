"""Tests for the publish-pool orchestration (src/publisher/run.py).

Everything here is offline: `fetch`, `client_factory`, `alert`, `sleep` and
`clock` are injected, `tmp_path` is the data directory, and the taxonomy and
JSON Schemas are the real vendored files — so the payloads these tests build
are validated exactly as a live run validates them.

The clock is a single injected UTC clock that `sleep` advances, which is what
makes "retries every 300s for two hours" a deterministic assertion rather than
a two-hour test.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.models import SourceItem
from src.pipeline.storage import RunLockHeldError, run_lock
from src.publisher.client import PoolApiError
from src.publisher.contract import validator
from src.publisher.run import (
    PublishOptions,
    PublishOutcome,
    TargetOutcome,
    acquire_lock_with_retry,
    mint_run_id,
    publish_pool,
)
from src.publisher.snapshots import load_snapshot, pool_dir
from src.publisher.taxonomy import load_taxonomy

START = datetime(2026, 9, 6, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Clock:
    """A deterministic UTC clock. `sleep()` advances it and records the wait."""

    def __init__(self, start=START):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.tick(seconds)

    def tick(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _config(taxonomy_version=1, schema_versions=(1,), batch_size=200, sources=None):
    return {
        "taxonomy_version": taxonomy_version,
        "schema_versions": list(schema_versions),
        "batch_size": batch_size,
        "sources": sources or {},
    }


class FakeClient:
    """The four ingest calls, recording every request in order.

    `rejected` is keyed by batch number. An entry is either a ready-made
    rejection dict, or a `(index, reason)` pair naming an item of *that*
    payload — which is how a test refuses a `key_v2` the run actually built,
    and so the only kind of refusal the repost can act on.

    A second post under a batch number already seen is the repost: it answers
    with `retry_rejected` and raises `retry_error`, so a test can say what the
    first pass refused and what the second pass made of it separately.
    """

    def __init__(
        self,
        env,
        *,
        config=None,
        config_error=None,
        batch_error=None,
        batch_error_on=1,
        manifest_error=None,
        request_charge=12.5,
        rejected=None,
        retry_rejected=None,
        retry_error=None,
    ):
        self.env = env
        self.calls: list[tuple] = []
        self.payloads: dict[str, list] = {"batch": [], "artists": [], "manifest": []}
        self._config = config or _config()
        self._config_error = config_error
        self._batch_error = batch_error
        self._batch_error_on = batch_error_on
        self._manifest_error = manifest_error
        self._request_charge = request_charge
        self._rejected = rejected or {}
        self._retry_rejected = retry_rejected or []
        self._retry_error = retry_error
        self._posted_batch_nos: set[int] = set()

    def get_config(self):
        self.calls.append(("config", None))
        if self._config_error:
            raise self._config_error
        return self._config

    @staticmethod
    def _rejections(payload, spec):
        entries = []
        for entry in spec:
            if isinstance(entry, dict):
                entries.append(entry)
                continue
            index, reason = entry
            item = payload["items"][index]
            entries.append(
                {"key_v2": item["key_v2"], "family": item["families"][0], "reason": reason}
            )
        return entries

    def post_batch(self, payload):
        batch_no = payload["batch_no"]
        repost = batch_no in self._posted_batch_nos
        self._posted_batch_nos.add(batch_no)
        self.calls.append(("batch", batch_no))
        self.payloads["batch"].append(payload)
        if repost:
            if self._retry_error:
                raise self._retry_error
            rejected = self._rejections(payload, self._retry_rejected)
        else:
            if self._batch_error and batch_no == self._batch_error_on:
                raise self._batch_error
            rejected = self._rejections(payload, self._rejected.get(batch_no, []))
        return {
            "run_id": payload["run_id"],
            "batch_no": batch_no,
            "upserted": len(payload["items"]) - len(rejected),
            "updated": 1,
            "unchanged": 2,
            "obsolete": 3,
            "rejected": rejected,
            "request_charge": self._request_charge,
        }

    def post_artists(self, payload):
        self.calls.append(("artists", payload["family"]))
        self.payloads["artists"].append(payload)
        return {"family": payload["family"], "artists": len(payload["artists"]), "updated": True}

    def post_manifest(self, payload):
        self.calls.append(("manifest", payload["batches"]))
        self.payloads["manifest"].append(payload)
        if self._manifest_error:
            raise self._manifest_error
        return {
            "run_id": payload["run_id"],
            "complete": True,
            "latest_advanced": True,
            "missing_batches": [],
        }


def _factory(clients):
    def make(settings, env):
        return clients[env]
    return make


def _settings(tmp_path, *, targets=("dev",), credentials=True, batch_size=200):
    data = {
        "data_dir": str(tmp_path),
        "sources": {
            "beatport": {"enabled": True},
            "bandcamp": {"enabled": True},
            "volumo": {"enabled": True},
            "soundcloud": {"enabled": True},
            "traxsource": {"enabled": False},
        },
    }
    settings = MagicMock()
    settings._data = data
    settings.data_dir = str(tmp_path)
    settings.source_enabled = lambda name: data["sources"].get(name, {}).get("enabled", False)
    settings.pool_taxonomy_version = 1
    settings.pool_batch_size = batch_size
    settings.pool_targets = list(targets)
    settings.pool_snapshot_retention_days = 14
    settings.pool_artist_weeks = 13
    settings.pool_lock_retry_seconds = 300
    settings.pool_lock_wait_max_seconds = 7200
    settings.pool_api_url = lambda env: f"https://api-{env}.example.test" if credentials else ""
    settings.pool_tenant = "t.onmicrosoft.com" if credentials else ""
    settings.pool_client_id = "cid" if credentials else ""
    settings.pool_client_secret = "sec" if credentials else ""
    settings.pool_scope = "api://x/.default" if credentials else ""
    settings.pool_token_url = "https://t.ciamlogin.com/t/oauth2/v2.0/token"
    return settings


def _volumo_item(n=0):
    return SourceItem(
        source="volumo",
        artist=f"Prunk {n}",
        title=f"Get Down {n}",
        link=f"https://volumo.com/track/c7f0a1e{n}",
        label="PIV",
        release_date="2026-08-21",
        release_name=f"Get Down {n}",
        genre_tags=[],
        raw_metadata={
            "volumo_track_id": f"c7f0a1e{n}",
            "volumo_genre_id": 21,
            "version": "Original Mix",
            "bpm": 126,
            "keysign": "A Minor",
            "chart_position": 12,
        },
    )


def _beatport_item():
    return SourceItem(
        source="beatport",
        artist="Borai & Denham Audio",
        title="Make Me",
        link="https://www.beatport.com/track/make-me/19283746",
        label="Columbia",
        release_date="2026-08-28",
        release_name="Make Me",
        genre_tags=[],
        raw_metadata={
            "beatport_id": 19283746,
            "genre_slug": "tech-house",
            "mix_name": "Franky Rizardo Extended Remix",
            "bpm": 130,
            "chart_position": 7,
        },
    )


def _health(**counts):
    return {name: {"count": count, "error": None} for name, count in counts.items()}


def _fetch(items, health=None, clock=None, seconds=0):
    """A fetch stub that records the settings it was handed."""
    calls = []

    def fetch(settings):
        calls.append(settings)
        if clock is not None and seconds:
            clock.tick(seconds)
        return list(items), dict(health if health is not None else _health(volumo=len(items)))

    fetch.calls = calls
    return fetch


def _run(settings, clock, *, envs=("dev",), clients=None, fetch=None, alert=None,
         dry_run=False, replay=None, taxonomy=None):
    clients = clients if clients is not None else {"dev": FakeClient("dev")}
    return publish_pool(
        settings,
        PublishOptions(envs=list(envs), dry_run=dry_run, replay_run_id=replay),
        clock=clock,
        sleep=clock.sleep,
        fetch=fetch if fetch is not None else _fetch([_volumo_item()]),
        client_factory=_factory(clients),
        alert=alert if alert is not None else MagicMock(),
        taxonomy=taxonomy,
    )


# ---------------------------------------------------------------------------
# Run ids
# ---------------------------------------------------------------------------

def test_mint_run_id_matches_contract_pattern():
    pattern = validator("batch").schema["$defs"]["run_id"]["pattern"]
    run_id = mint_run_id(START)

    assert run_id.startswith("2026-09-06T06:00:00Z-")
    assert re.match(pattern, run_id), run_id
    # Two runs minted in the same second are still distinguishable.
    assert mint_run_id(START) != run_id


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

def test_acquire_lock_with_retry_takes_the_lock_when_free(tmp_path):
    clock = Clock()
    with acquire_lock_with_retry(str(tmp_path), 300, 7200, sleep=clock.sleep,
                                 clock=lambda: clock().timestamp()):
        with pytest.raises(RunLockHeldError):
            with run_lock(str(tmp_path)):
                pass
    assert clock.sleeps == []


def test_lock_held_retries_every_300s_then_skips_after_7200s_with_alert(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    client = FakeClient("dev")
    fetch = _fetch([_volumo_item()])

    with run_lock(str(tmp_path)):
        outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                       alert=alert, taxonomy=taxonomy)

    assert clock.sleeps == [300] * 24
    assert outcome.skipped is True
    assert outcome.skip_reason == "lock_held"
    assert fetch.calls == []
    assert [c for c in client.calls if c[0] != "config"] == []
    assert alert.call_count == 1
    assert "lock" in alert.call_args[0][0].lower()


def test_lock_released_before_posting(tmp_path, taxonomy):
    """The lock covers the fetch only — posting must not hold it."""
    settings = _settings(tmp_path)
    clock = Clock()
    client = FakeClient("dev")
    acquired = []

    original_post_batch = client.post_batch

    def post_batch(payload):
        with run_lock(str(tmp_path)):
            acquired.append(payload["batch_no"])
        return original_post_batch(payload)

    client.post_batch = post_batch

    outcome = _run(settings, clock, clients={"dev": client}, taxonomy=taxonomy)

    assert acquired == [1]
    assert outcome.ok is True


# ---------------------------------------------------------------------------
# The config gate
# ---------------------------------------------------------------------------

def test_config_taxonomy_mismatch_skips_with_alert_before_fetch(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    fetch = _fetch([_volumo_item()])
    client = FakeClient("dev", config=_config(taxonomy_version=99))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    assert outcome.skipped is True
    assert outcome.skip_reason == "taxonomy_version_mismatch"
    assert fetch.calls == []
    assert client.payloads["batch"] == []
    assert alert.call_count == 1


def test_config_schema_version_missing_skips(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    fetch = _fetch([_volumo_item()])
    client = FakeClient("dev", config=_config(schema_versions=(2,)))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    assert outcome.skipped is True
    assert outcome.skip_reason == "schema_version_unsupported"
    assert fetch.calls == []
    assert alert.call_count == 1


def test_config_unreachable_skips(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    fetch = _fetch([_volumo_item()])
    client = FakeClient("dev", config_error=PoolApiError(None, "transport", "ConnectionError"))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    assert outcome.skipped is True
    assert outcome.skip_reason == "config_unreachable"
    assert fetch.calls == []
    assert alert.call_count == 1


def test_fetch_false_source_excluded_and_reported_disabled(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    client = FakeClient(
        "dev",
        config=_config(sources={"beatport": {"fetch": False, "display": True, "preview": True}}),
    )
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    fetch_settings = fetch.calls[0]
    assert isinstance(fetch_settings, Settings)
    assert fetch_settings.source_enabled("beatport") is False
    assert fetch_settings.source_enabled("volumo") is True
    # The publisher's own settings are never mutated.
    assert settings._data["sources"]["beatport"]["enabled"] is True

    manifest = client.payloads["manifest"][0]
    assert manifest["per_source"]["beatport"] == {"count": 0, "error": None, "enabled": False}
    assert manifest["per_source"]["volumo"]["count"] == 1
    assert outcome.per_source["beatport"]["enabled"] is False


def test_fetch_false_source_still_fetched_when_the_other_target_wants_it(tmp_path, taxonomy):
    """Two targets: a source is fetched when either wants it, and each manifest
    reports its own switch."""
    settings = _settings(tmp_path, targets=("dev", "prod"))
    clock = Clock()
    clients = {
        "dev": FakeClient(
            "dev",
            config=_config(sources={"beatport": {"fetch": False, "display": True, "preview": True}}),
        ),
        "prod": FakeClient("prod"),
    }
    fetch = _fetch([_beatport_item(), _volumo_item()], health=_health(beatport=1, volumo=1))

    _run(settings, clock, envs=("dev", "prod"), clients=clients, fetch=fetch, taxonomy=taxonomy)

    assert fetch.calls[0].source_enabled("beatport") is True
    dev_manifest = clients["dev"].payloads["manifest"][0]
    prod_manifest = clients["prod"].payloads["manifest"][0]
    assert dev_manifest["per_source"]["beatport"] == {"count": 0, "error": None, "enabled": False}
    assert prod_manifest["per_source"]["beatport"]["enabled"] is True
    assert prod_manifest["per_source"]["beatport"]["count"] == 1


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def test_posts_batches_in_order_then_artists_then_manifest(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=200)
    clock = Clock()
    client = FakeClient("dev")
    items = [_volumo_item(n) for n in range(250)]
    fetch = _fetch(items, health=_health(volumo=250))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    assert outcome.items == 250
    assert outcome.batches == 2
    kinds = [kind for kind, _ in client.calls]
    assert kinds[0] == "config"
    assert kinds[1:3] == ["batch", "batch"]
    assert kinds[-1] == "manifest"
    assert set(kinds[3:-1]) == {"artists"}
    assert [no for kind, no in client.calls if kind == "batch"] == [1, 2]
    assert [len(p["items"]) for p in client.payloads["batch"]] == [200, 50]
    assert client.payloads["manifest"][0]["batches"] == 2


def test_batch_size_is_the_smaller_of_settings_and_config(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=200)
    clock = Clock()
    client = FakeClient("dev", config=_config(batch_size=100))
    fetch = _fetch([_volumo_item(n) for n in range(150)], health=_health(volumo=150))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    assert outcome.batches == 2
    assert [len(p["items"]) for p in client.payloads["batch"]] == [100, 50]


def test_counts_summed_and_rejections_tallied(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    rejected = {
        1: [{"key_v2": "a||b", "family": "house", "reason": "throttled"}],
        2: [
            {"key_v2": "c||d", "family": "house", "reason": "throttled"},
            {"key_v2": "e||f", "family": "dnb", "reason": "store_error"},
        ],
    }
    client = FakeClient("dev", rejected=rejected, request_charge=10.0)
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    target = outcome.targets[0]
    assert target.batches_acked == 2
    assert target.upserted == 1 + 0
    assert target.updated == 2
    assert target.unchanged == 4
    assert target.obsolete == 6
    assert target.rejected == 3
    assert target.rejected_reasons == {"throttled": 2, "store_error": 1}
    assert target.request_charge == 20.0


# ---------------------------------------------------------------------------
# The `throttled` repost
# ---------------------------------------------------------------------------

def test_throttled_copies_are_reposted_once_at_the_end_of_the_run(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    client = FakeClient("dev", rejected={1: [(0, "throttled")]})
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    kinds = [kind for kind, _ in client.calls]
    assert kinds[0] == "config"
    assert kinds[1:4] == ["batch", "batch", "batch"]
    assert set(kinds[4:-1]) == {"artists"}
    assert kinds[-1] == "manifest"

    refused = client.payloads["batch"][0]["items"][0]["key_v2"]
    repost = client.payloads["batch"][2]
    assert [item["key_v2"] for item in repost["items"]] == [refused]
    assert repost["batch_no"] == 1
    assert outcome.targets[0].retried == 1
    assert clock.sleeps == [5]


def test_throttled_repost_does_not_double_count_the_run(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    client = FakeClient("dev", rejected={1: [(0, "throttled")]})
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    target = outcome.targets[0]
    # Batch 1 (1 upserted of 2) and batch 2 (2 upserted), and nothing else.
    assert target.batches_acked == 2
    assert target.upserted == 3
    assert target.updated == 2
    assert target.unchanged == 4
    assert target.obsolete == 6
    assert target.request_charge == 25.0
    # The refusal happened, so it is still counted as one.
    assert target.rejected == 1
    assert target.rejected_reasons == {"throttled": 1}
    # The repost's own acknowledgement is kept apart.
    assert target.retried == 1
    assert target.retry_written == 2
    assert target.retry_rejected == 0
    assert target.retry_request_charge == 12.5


def test_store_error_and_conflict_are_not_reposted(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=4)
    clock = Clock()
    client = FakeClient(
        "dev",
        rejected={1: [(0, "throttled"), (1, "store_error"), (2, "conflict")]},
    )
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    posted = client.payloads["batch"]
    assert len(posted) == 2
    throttled = posted[0]["items"][0]["key_v2"]
    assert [item["key_v2"] for item in posted[1]["items"]] == [throttled]
    assert outcome.targets[0].retried == 1


def test_a_repost_that_is_throttled_again_is_not_retried_again(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    client = FakeClient(
        "dev", rejected={1: [(0, "throttled")]}, retry_rejected=[(0, "throttled")]
    )
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    assert len([kind for kind, _ in client.calls if kind == "batch"]) == 3
    assert outcome.targets[0].retry_rejected == 1
    assert outcome.ok is True


def test_repost_failure_does_not_fail_the_run_or_skip_the_manifest(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    alert = MagicMock()
    client = FakeClient(
        "dev",
        rejected={1: [(0, "throttled")]},
        retry_error=PoolApiError(503, "transport", "503"),
    )
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    target = outcome.targets[0]
    assert len(client.payloads["manifest"]) == 1
    assert client.calls[-1][0] == "manifest"
    assert target.error is None
    assert outcome.ok is True
    alert.assert_not_called()


def test_nothing_throttled_posts_no_retry_batch(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    client = FakeClient("dev")
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    assert [no for kind, no in client.calls if kind == "batch"] == [1, 2]
    assert outcome.targets[0].retried == 0
    assert clock.sleeps == []


def test_snapshot_records_the_retry(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=2)
    clock = Clock()
    client = FakeClient(
        "dev", rejected={1: [(0, "throttled")]}, retry_rejected=[(0, "throttled")]
    )
    fetch = _fetch([_volumo_item(n) for n in range(4)], health=_health(volumo=4))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    record = load_snapshot(pool_dir(str(tmp_path)), outcome.run_id)["targets"]["dev"]
    assert record["retried"] == 1
    assert record["retry_rejected"] == 1


def test_batch_failure_after_retries_skips_manifest_alerts_and_continues_to_next_target(
    tmp_path, taxonomy
):
    settings = _settings(tmp_path, targets=("dev", "prod"), batch_size=1)
    clock = Clock()
    alert = MagicMock()
    clients = {
        "dev": FakeClient("dev", batch_error=PoolApiError(503, "transport", "503"),
                          batch_error_on=2),
        "prod": FakeClient("prod"),
    }
    fetch = _fetch([_volumo_item(n) for n in range(3)], health=_health(volumo=3))

    outcome = _run(settings, clock, envs=("dev", "prod"), clients=clients, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    dev, prod = outcome.targets
    assert dev.env == "dev"
    assert dev.batches_acked == 1
    assert dev.error is not None
    assert clients["dev"].payloads["manifest"] == []
    assert [no for kind, no in clients["dev"].calls if kind == "batch"] == [1, 2]

    assert prod.error is None
    assert prod.batches_acked == 3
    assert clients["prod"].payloads["manifest"][0]["batches"] == 3

    assert outcome.ok is False
    assert alert.call_count == 1
    assert "dev" in alert.call_args[0][0]


def test_unexpected_exception_in_post_phase_alerts_and_continues_to_next_target(
    tmp_path, taxonomy
):
    """A `PoolApiError` is not the only way the post phase can fail — a bad
    `resp.json()` or any other surprise must not blow up the whole run,
    escape with no alert, and leave the other target unposted."""
    settings = _settings(tmp_path, targets=("dev", "prod"))
    clock = Clock()
    alert = MagicMock()
    dev_client = FakeClient("dev")

    def _boom(payload):
        raise RuntimeError("boom")

    dev_client.post_batch = _boom
    clients = {"dev": dev_client, "prod": FakeClient("prod")}
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, envs=("dev", "prod"), clients=clients, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    dev, prod = outcome.targets
    assert dev.env == "dev"
    assert dev.error is not None
    assert "RuntimeError" in dev.error
    assert "boom" in dev.error

    assert prod.error is None
    assert prod.manifest is not None and prod.manifest["complete"] is True

    assert outcome.ok is False
    assert alert.call_count == 1
    text = alert.call_args[0][0]
    assert "dev" in text
    assert "://" not in text

    snapshot = load_snapshot(pool_dir(str(tmp_path)), outcome.run_id)
    assert snapshot["targets"]["dev"]["error"] is not None


def test_manifest_409_recorded_with_missing_batches(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    client = FakeClient(
        "dev",
        manifest_error=PoolApiError(409, "batches_missing", "batch 2 was never acknowledged", [2]),
    )
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    target = outcome.targets[0]
    assert target.error is not None
    assert "batches_missing" in target.error
    assert "[2]" in target.error
    assert target.latest_advanced is False
    assert outcome.ok is False
    assert alert.call_count == 1


def test_two_targets_independent(tmp_path, taxonomy):
    settings = _settings(tmp_path, targets=("dev", "prod"))
    clock = Clock()
    clients = {
        "dev": FakeClient("dev", batch_error=PoolApiError(500, "store_error", "boom")),
        "prod": FakeClient("prod"),
    }
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, envs=("dev", "prod"), clients=clients, fetch=fetch,
                   taxonomy=taxonomy)

    dev, prod = outcome.targets
    assert dev.error is not None and dev.manifest is None
    assert prod.error is None and prod.manifest["complete"] is True
    assert prod.latest_advanced is True
    assert fetch.calls and len(fetch.calls) == 1  # one fetch feeds both targets


def test_manifest_that_did_not_advance_freshness_alerts_informationally(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    client = FakeClient("dev")
    client.post_manifest = lambda payload: {
        "run_id": payload["run_id"], "complete": True,
        "latest_advanced": False, "missing_batches": [],
    }
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    assert outcome.ok is True
    assert outcome.targets[0].latest_advanced is False
    assert alert.call_count == 1
    assert "later run" in alert.call_args[0][0]


# ---------------------------------------------------------------------------
# Dry run and replay
# ---------------------------------------------------------------------------

def test_dry_run_posts_nothing_alerts_nothing_writes_snapshot(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    client = FakeClient("dev")
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, dry_run=True, taxonomy=taxonomy)

    assert client.payloads["batch"] == []
    assert client.payloads["manifest"] == []
    assert client.payloads["artists"] == []
    assert alert.call_count == 0
    assert outcome.targets == []
    assert outcome.items == 1
    assert outcome.snapshot_path is not None and os.path.exists(outcome.snapshot_path)
    snapshot = load_snapshot(pool_dir(str(tmp_path)), outcome.run_id)
    assert len(snapshot["items"]) == 1
    assert snapshot["targets"] == {}


def test_dry_run_without_credentials_skips_config_call(tmp_path, taxonomy):
    settings = _settings(tmp_path, credentials=False)
    clock = Clock()
    client = FakeClient("dev")
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   dry_run=True, taxonomy=taxonomy)

    assert client.calls == []
    assert outcome.skipped is False
    assert outcome.items == 1
    # Every switch is True when the config was never read.
    assert outcome.per_source["beatport"]["enabled"] is True


def test_replay_uses_snapshot_run_id_and_items_and_no_fetch_no_lock(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    first = _run(settings, clock, clients={"dev": FakeClient("dev")},
                 fetch=_fetch([_volumo_item()], health=_health(volumo=1)),
                 dry_run=True, taxonomy=taxonomy)

    replay_client = FakeClient("dev")
    replay_fetch = _fetch([_volumo_item(9)])
    clock.tick(3600)

    with run_lock(str(tmp_path)):  # a replay never takes the lock
        outcome = _run(settings, clock, clients={"dev": replay_client}, fetch=replay_fetch,
                       replay=first.run_id, taxonomy=taxonomy)

    assert replay_fetch.calls == []
    assert outcome.run_id == first.run_id
    assert outcome.started_at == first.started_at
    assert outcome.items == 1
    assert replay_client.payloads["batch"][0]["run_id"] == first.run_id
    manifest = replay_client.payloads["manifest"][0]
    assert manifest["started_at"] == first.started_at
    assert manifest["completed_at"] > first.started_at
    assert outcome.ok is True


def test_replay_does_not_rewrite_the_snapshot_items(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    first = _run(settings, clock, clients={"dev": FakeClient("dev")},
                 fetch=_fetch([_volumo_item()], health=_health(volumo=1)),
                 dry_run=True, taxonomy=taxonomy)
    before = load_snapshot(pool_dir(str(tmp_path)), first.run_id)["items"]

    _run(settings, clock, clients={"dev": FakeClient("dev")}, replay=first.run_id,
         taxonomy=taxonomy)

    after = load_snapshot(pool_dir(str(tmp_path)), first.run_id)
    assert after["items"] == before
    assert after["targets"]["dev"]["batches_acked"] == 1


# ---------------------------------------------------------------------------
# Nothing to publish
# ---------------------------------------------------------------------------

def test_no_items_skips_with_alert(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    client = FakeClient("dev")
    fetch = _fetch([], health={"volumo": {"count": 0, "error": "boom"}})

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch,
                   alert=alert, taxonomy=taxonomy)

    assert outcome.skipped is True
    assert outcome.skip_reason == "no_items"
    assert client.payloads["batch"] == []
    assert client.payloads["manifest"] == []
    assert alert.call_count == 1
    assert outcome.snapshot_path is not None


def test_alert_text_has_no_url(tmp_path, taxonomy):
    """`requests` puts the request url in every HTTPError message; no alert may
    carry one (CONTRACTS §8 — the same rule the status page's `error` obeys)."""
    settings = _settings(tmp_path)
    clock = Clock()
    alert = MagicMock()
    error = (
        "500 Server Error: Internal Server Error for url: "
        "https://api.beatport.com/v4/catalog/genres/6/tracks?page=1"
    )
    fetch = _fetch([], health={"beatport": {"count": 0, "error": error}})

    _run(settings, clock, clients={"dev": FakeClient("dev")}, fetch=fetch,
         alert=alert, taxonomy=taxonomy)

    text = alert.call_args[0][0]
    assert "<url>" in text
    assert "https://" not in text


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

def test_snapshot_targets_updated_after_each_ack(tmp_path, taxonomy):
    settings = _settings(tmp_path, batch_size=1)
    clock = Clock()
    client = FakeClient("dev")
    seen = []

    original_post_batch = client.post_batch

    def post_batch(payload):
        body = original_post_batch(payload)
        # Read the snapshot back as it stands *before* this batch's ack lands.
        snapshot = load_snapshot(pool_dir(str(tmp_path)), payload["run_id"])
        seen.append(snapshot.get("targets", {}).get("dev", {}).get("batches_acked"))
        return body

    client.post_batch = post_batch
    fetch = _fetch([_volumo_item(n) for n in range(3)], health=_health(volumo=3))

    outcome = _run(settings, clock, clients={"dev": client}, fetch=fetch, taxonomy=taxonomy)

    assert seen == [None, 1, 2]
    final = load_snapshot(pool_dir(str(tmp_path)), outcome.run_id)
    assert final["targets"]["dev"]["batches_acked"] == 3
    assert final["targets"]["dev"]["complete"] is True
    assert final["targets"]["dev"]["error"] is None


def test_publisher_never_writes_outside_data_pool(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    _run(settings, clock, clients={"dev": FakeClient("dev")}, fetch=fetch, taxonomy=taxonomy)

    entries = set(os.listdir(tmp_path))
    # The run lock is the one file outside pool/ the publisher touches, and it
    # is TuneFinder's existing lock, not a store.
    assert entries <= {"pool", ".tunefinder.lock"}, entries
    for absent in ("source_items.json", "archive", "source_health.json"):
        assert not os.path.exists(os.path.join(tmp_path, absent))
    assert os.path.exists(os.path.join(tmp_path, "pool", "health.json"))
    assert os.path.exists(os.path.join(tmp_path, "pool", "artist_index.json"))


def test_health_log_records_the_run(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    clock = Clock()
    fetch = _fetch([_volumo_item()], health=_health(volumo=1))

    outcome = _run(settings, clock, clients={"dev": FakeClient("dev")}, fetch=fetch,
                   taxonomy=taxonomy)

    with open(os.path.join(tmp_path, "pool", "health.json")) as f:
        entries = json.load(f)
    assert entries[-1]["run_id"] == outcome.run_id
    assert entries[-1]["per_source"]["volumo"]["count"] == 1


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_pool_settings_from_another_taxonomy_is_a_value_error(tmp_path, taxonomy):
    settings = _settings(tmp_path)
    settings.pool_taxonomy_version = 99
    clock = Clock()

    with pytest.raises(ValueError, match="taxonomy"):
        _run(settings, clock, clients={"dev": FakeClient("dev")}, taxonomy=taxonomy)


# ---------------------------------------------------------------------------
# The summary line
# ---------------------------------------------------------------------------

def test_summary_line_shape():
    outcome = PublishOutcome(
        run_id="2026-09-06T06:00:00Z-a3f9c1",
        started_at="2026-09-06T06:00:00Z",
        items=1234,
        batches=7,
        fetch_seconds=45.6,
        total_seconds=61.0,
        artist_payloads=57,
        targets=[
            TargetOutcome(
                env="dev",
                base_url="https://api-dev.example.test",
                batches_acked=7,
                upserted=100,
                updated=20,
                unchanged=1000,
                obsolete=4,
                rejected=2,
                request_charge=4321.0,
                post_seconds=12.3,
                artists_posted=55,
            )
        ],
    )

    assert outcome.summary_line() == (
        "publish-pool 2026-09-06T06:00:00Z-a3f9c1 — 1234 items in 7 batches; "
        "dev: upserted 100 updated 20 unchanged 1000 obsolete 4 rejected 2, "
        "RU 4321.0 (3.50/item), post 12.3s, artists 55/57; fetch 45.6s; total 61.0s"
    )


def test_summary_line_names_the_repost():
    def line(retried):
        return PublishOutcome(
            run_id="2026-09-06T06:00:00Z-a3f9c1",
            started_at="2026-09-06T06:00:00Z",
            items=1234,
            batches=7,
            artist_payloads=57,
            targets=[
                TargetOutcome(
                    env="dev",
                    base_url="https://api-dev.example.test",
                    batches_acked=7,
                    artists_posted=55,
                    retried=retried,
                )
            ],
        ).summary_line()

    assert "artists 55/57, reposted 1;" in line(1)
    assert "reposted" not in line(0)


def test_summary_line_says_why_a_run_was_skipped():
    outcome = PublishOutcome(
        run_id="2026-09-06T06:00:00Z-a3f9c1",
        started_at="2026-09-06T06:00:00Z",
        skipped=True,
        skip_reason="lock_held",
    )
    assert outcome.summary_line() == (
        "publish-pool 2026-09-06T06:00:00Z-a3f9c1 — skipped: lock_held"
    )
    assert outcome.ok is False
