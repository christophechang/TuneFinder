"""The Volumo preview check — M1d's open point 6.

Volumo refuses the prelisten for a large share of new releases (402: about half
of drum & bass, a tenth of house), and a dead preview in the booth's deck costs
a load timeout and a hold. So at publish, every item whose chosen preview is
`volumo_prelisten` gets one `HEAD` of the prelisten url **with the pinned `c`
token**, and `preview.eligible` says what Volumo answered:

  * 2xx → eligible;
  * any 4xx → not eligible (402 is the common one; 400 means Volumo refused the
    token itself, which the run alerts on);
  * a network error or 5xx → not eligible for this run, and checked again first
    next run.

Beatport's `sample_url` is not checked, and neither is any other kind: only
`preview_for`'s Volumo choice. The ref sent to the API stays token-free — the
web adapter appends its own `c`.

Politeness: one request at a time over one connection, a short delay between
requests, at most `CHECK_CAP` per run and `TIME_BUDGET` of wall clock, and a
stop after `BREAKER_AFTER` failures in a row. A run carries ~6,500 Volumo
previews; about 150 are new on a normal day and ~1,500 on the weekly chart
refresh. So verdicts are cached in `pool/volumo_previews.json` and re-checked
after `RECHECK_AFTER`; never-checked tracks go first, then provisional ones,
then the stalest. Over the cap a track keeps its cached verdict, or stays
eligible if it has none (or only a provisional one), and the log says how many.

A single 400 is a verdict on the track (S6 saw a few per genre with a valid
token). `TOKEN_STREAK` of them in a row is Volumo refusing the token: the check
stops, those verdicts are kept provisional, and the run alerts.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from src.logger import get_logger
from src.pipeline.storage import atomic_write_json
from src.publisher import PUBLISHER_VERSION

logger = get_logger(__name__)

LOG = "[publish-pool]"

# The same value as VOLUMO_PREVIEW_TOKEN in tunefinder-multi-tenant's
# web/src/reports/volumoPreview.ts (moving to the booth's transport in M3b Task
# 6). Not a secret or a signature: Volumo's own player computes it client-side
# and it is the same for every track. If Volumo changes the scheme every check
# answers 400, the run stops checking and alerts; paste the new value in both
# places.
VOLUMO_PREVIEW_TOKEN = "4dm1hpc2"

CHECK_CAP = 1500
CHECK_DELAY_SECONDS = 0.25
TIME_BUDGET = timedelta(minutes=20)
RECHECK_AFTER = timedelta(days=7)
# A verdict stamped late in yesterday-week's check is still due today, rather
# than slipping a day because it is a few minutes short of seven.
_RECHECK_SLACK = timedelta(hours=12)
BREAKER_AFTER = 5
TOKEN_STREAK = 5
REQUEST_TIMEOUT_SECONDS = 10

# 4xx answers about the request rather than the track (forbidden, method not
# allowed, timeout, rate limited): provisional, like a 5xx.
_PROVISIONAL_4XX = {403, 405, 408, 429}

CACHE_FILE = "volumo_previews.json"
# The pool's own expiry. A track still in the pool is re-checked weekly, so its
# entry never gets this old; one that is not has left the pool.
CACHE_KEEP = timedelta(days=45)

_KIND = "volumo_prelisten"
_ERROR = "error"
_TOKEN = "token"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_BUCKETS = ("eligible", "ineligible", "error", "unchecked")

_session = requests.Session()
_session.headers["User-Agent"] = f"tunefinder-publisher/{PUBLISHER_VERSION}"


@dataclass
class PreviewCheck:
    """What one run's check did. Stored in the snapshot as-is.

    `checked`, `cached` and `unchecked` count tracks; `by_family` counts items
    per family, so an item in two families counts twice there. `error` in
    `by_family` is a provisional verdict: ineligible this run, checked again.
    """

    checked: int = 0
    cached: int = 0
    unchecked: int = 0
    token_rejected: bool = False
    stopped: str | None = None
    by_status: dict[str, int] = field(default_factory=dict)
    by_family: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary(self) -> str:
        text = (
            f"volumo previews: checked {self.checked} cached {self.cached} "
            f"unchecked {self.unchecked}"
        )
        if self.stopped:
            text += f" (stopped: {self.stopped})"
        return text


def default_head(url: str) -> int:
    """One `HEAD`, redirects followed; the status code. Raises on a network error."""
    response = _session.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT_SECONDS)
    return response.status_code


def check_volumo_previews(
    items: list[dict],
    cache: dict[str, dict],
    *,
    head=default_head,
    clock,
    sleep,
    cap: int = CHECK_CAP,
    delay: float = CHECK_DELAY_SECONDS,
) -> PreviewCheck:
    """Set `preview.eligible` and `checked_at` on every Volumo-preview item.

    Mutates `items` (freshly built contract dicts) and `cache` in place. Never
    raises for a request that went wrong: that is an ineligible preview, not a
    failed run.
    """
    result = PreviewCheck()
    by_track: dict[str, list[dict]] = {}
    for item in items:
        preview = item.get("preview")
        if preview and preview.get("kind") == _KIND:
            by_track.setdefault(_track_id(preview["ref"]), []).append(item)
    if not by_track:
        return result

    started = clock()
    queue = sorted(
        (track_id for track_id in by_track if _needs_check(cache.get(track_id), started)),
        key=lambda track_id: _queue_rank(cache.get(track_id)),
    )

    failures_in_a_row = 0
    rejected_in_a_row: list[str] = []
    attempted = 0
    for track_id in queue:
        if attempted >= cap:
            break
        if failures_in_a_row >= BREAKER_AFTER:
            result.stopped = f"{BREAKER_AFTER} failures in a row"
            break
        if clock() - started >= TIME_BUDGET:
            result.stopped = "time budget spent"
            break
        if attempted:
            sleep(delay)
        attempted += 1

        status = _head_status(head, _check_url(by_track[track_id][0]["preview"]["ref"]))
        cache[track_id] = {
            "eligible": status.isdigit() and 200 <= int(status) < 300,
            "status": status,
            "checked_at": clock().astimezone(timezone.utc).strftime(_TIME_FORMAT),
        }
        result.checked += 1
        result.by_status[status] = result.by_status.get(status, 0) + 1
        failures_in_a_row = failures_in_a_row + 1 if _provisional(status) else 0
        rejected_in_a_row = rejected_in_a_row + [track_id] if status == "400" else []
        if len(rejected_in_a_row) >= TOKEN_STREAK:
            # Not five dead tracks: Volumo refusing the token. Keep them
            # provisional so a fixed token re-checks them first.
            for rejected in rejected_in_a_row:
                cache[rejected]["status"] = _TOKEN
            result.token_rejected = True
            result.stopped = f"{TOKEN_STREAK} token refusals (400) in a row"
            break

    if result.stopped:
        logger.warning("%s volumo preview check stopped: %s", LOG, result.stopped)

    checked = set(queue[:attempted])
    for track_id, track_items in by_track.items():
        entry = cache.get(track_id)
        if track_id in checked:
            bucket = _bucket(entry)
        elif entry is None or _provisional(entry["status"]):
            # A failure is ineligible for the run that saw it, not for later
            # runs that could not reach the track again.
            result.unchecked += 1
            bucket = "unchecked"
        else:
            result.cached += 1
            bucket = _bucket(entry)
        if bucket != "unchecked":
            _apply(track_items, entry)
        for item in track_items:
            for family in item.get("families") or []:
                counts = result.by_family.setdefault(family, dict.fromkeys(_BUCKETS, 0))
                counts[bucket] += 1
    result.by_family = dict(sorted(result.by_family.items()))
    return result


def load_preview_cache(pool_dir_path: str) -> dict[str, dict]:
    """The verdict cache, or empty when it is missing or unreadable — a lost
    cache costs requests, never a run."""
    path = os.path.join(pool_dir_path, CACHE_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "%s volumo preview cache unreadable, starting empty: %s", LOG, type(exc).__name__
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return {track_id: entry for track_id, entry in data.items() if _well_formed(entry)}


def prune_preview_cache(cache: dict[str, dict], now: datetime) -> dict[str, dict]:
    cutoff = now - CACHE_KEEP
    kept = {}
    for track_id, entry in cache.items():
        checked_at = _parse(entry.get("checked_at"))
        if checked_at is not None and checked_at >= cutoff:
            kept[track_id] = entry
    return kept


def save_preview_cache(pool_dir_path: str, cache: dict[str, dict]) -> None:
    atomic_write_json(os.path.join(pool_dir_path, CACHE_FILE), cache, indent=0)


# ---------------------------------------------------------------------------

def _track_id(ref: str) -> str:
    # https://volumo.com/api/v1/tracks/<id>/prelisten.mp3
    return ref.rstrip("/").rsplit("/", 2)[-2]


def _check_url(ref: str) -> str:
    return f"{ref}?c={VOLUMO_PREVIEW_TOKEN}"


def _head_status(head, url: str) -> str:
    try:
        return str(int(head(url)))
    except Exception as exc:  # noqa: BLE001 — a check may never fail a run
        logger.debug("%s volumo preview check failed: %s", LOG, type(exc).__name__)
        return _ERROR


def _provisional(status: str) -> bool:
    """A verdict about the network or the request, not the track: ineligible
    for this run and re-checked next run."""
    if not status.isdigit():
        return True
    code = int(status)
    return code >= 500 or code in _PROVISIONAL_4XX


def _bucket(entry: dict) -> str:
    if _provisional(entry["status"]):
        return "error"
    return "eligible" if entry["eligible"] else "ineligible"


def _well_formed(entry) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("eligible"), bool)
        and isinstance(entry.get("status"), str)
        and _parse(entry.get("checked_at")) is not None
    )


def _needs_check(entry: dict | None, now: datetime) -> bool:
    if entry is None or _provisional(entry["status"]):
        return True
    return now - _parse(entry["checked_at"]) >= RECHECK_AFTER - _RECHECK_SLACK


def _queue_rank(entry: dict | None) -> tuple[int, str]:
    """Never checked, then provisional, then the stalest definitive verdict."""
    if entry is None:
        return (0, "")
    if _provisional(entry["status"]):
        return (1, entry["checked_at"])
    return (2, entry["checked_at"])


def _apply(items: list[dict], entry: dict) -> None:
    for item in items:
        item["preview"]["eligible"] = bool(entry["eligible"])
        item["preview"]["checked_at"] = entry["checked_at"]


def _parse(text) -> datetime | None:
    try:
        return datetime.strptime(text, _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
