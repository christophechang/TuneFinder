"""The exported shards -> one API target, through the publisher's PoolApiClient
(same Entra token, same retry and 429 handling as publish-pool).

Shards 0..total-1 in order, then the commit — and the commit only when every
shard was acknowledged. The API writes shards to its inactive slot and flips
only on a commit that counts them all, so a failure anywhere before the commit
leaves the live dictionary as it was.

A commit refused with 409 is read, not just reported (CONTRACTS §2):
`stale_version` naming our own version means the commit already landed and its
answer was lost, so it counts as committed; `incomplete_version` (a shard write
not yet visible) and `commit_conflict` (another commit won the manifest race)
are retried once after a short pause, then fail.
"""
import json
import os
import time
from dataclasses import dataclass

from src.logger import get_logger
from src.publisher.client import PoolApiError
from src.publisher.dictionary import LOG
from src.publisher.dictionary.export import COMMIT_FILE, shard_file

logger = get_logger(__name__)

COMMIT_RETRY_PAUSE_S = 5
_COMMIT_RETRIED = ("incomplete_version", "commit_conflict")


@dataclass
class UploadOutcome:
    env: str
    version: str | None = None
    total: int = 0
    shards_posted: int = 0
    stored: int = 0
    committed: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.committed and self.error is None

    def summary_line(self) -> str:
        line = (f"upload {self.env}: version {self.version}, shards {self.shards_posted}/{self.total}, "
                f"stored {self.stored}, {'committed' if self.committed else 'NOT committed'}")
        return line + (f", error: {self.error}" if self.error else "")


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _commit(client, commit: dict, env: str, sleep) -> None:
    for attempt in range(2):
        try:
            client.post_dictionary_commit(commit)
            return
        except PoolApiError as exc:
            if exc.status != 409:
                raise
            if exc.error == "stale_version" and commit["version"] in (exc.detail or ""):
                logger.info("%s upload %s: version %s is already active; the commit had landed",
                            LOG, env, commit["version"])
                return
            if exc.error in _COMMIT_RETRIED and attempt == 0:
                logger.warning("%s upload %s: commit refused (%s); retrying once in %ss",
                               LOG, env, exc.error, COMMIT_RETRY_PAUSE_S)
                sleep(COMMIT_RETRY_PAUSE_S)
                continue
            raise


def upload(client, export_dir: str, env: str, sleep=time.sleep) -> UploadOutcome:
    outcome = UploadOutcome(env=env)
    try:
        commit = _read(os.path.join(export_dir, COMMIT_FILE))
        outcome.version, outcome.total = commit["version"], commit["total"]
        for index in range(commit["total"]):
            payload = _read(os.path.join(export_dir, shard_file(index)))
            if payload.get("index") != index or payload.get("version") != commit["version"]:
                raise ValueError(f"{shard_file(index)} does not belong to version {commit['version']}")
            body = client.post_dictionary_shard(payload)
            outcome.shards_posted += 1
            outcome.stored += int(body.get("stored") or 0)
        _commit(client, commit, env, sleep)
        outcome.committed = True
    except PoolApiError as exc:
        outcome.error = str(exc)
    except (OSError, ValueError, KeyError) as exc:
        outcome.error = f"export unreadable: {type(exc).__name__}: {exc}"
    if outcome.error:
        logger.error("%s upload %s failed: %s", LOG, env, outcome.error)
    return outcome


def upload_to_env(settings, env: str, export_dir: str, client_factory=None) -> UploadOutcome:
    """Upload to `env` ("dev" / "prod") using the publisher's per-env settings."""
    if not settings.pool_api_url(env):
        outcome = UploadOutcome(env=env, error=f"no API url configured for {env}")
        logger.error("%s %s", LOG, outcome.error)
        return outcome
    if client_factory is None:
        from src.publisher.run import default_client_factory as client_factory
    return upload(client_factory(settings, env), export_dir, env)
