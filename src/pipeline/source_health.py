"""
Per-source run health persistence and anomaly detection.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from src.pipeline.storage import atomic_write_json

_HEALTH_FILE = "source_health.json"
_RETENTION = 26  # keep the most recent N runs (matches archive retention)


def append_run_health(health: dict, data_dir: str, report_id: str) -> None:
    """Append current run health; prune to the most recent _RETENTION entries."""
    prior = load_run_health(data_dir)
    entry = {
        "report_id": report_id,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "health": health,
    }
    combined = prior + [entry]
    if len(combined) > _RETENTION:
        combined = combined[-_RETENTION:]
    path = os.path.join(data_dir, _HEALTH_FILE)
    atomic_write_json(path, combined)


def load_run_health(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, _HEALTH_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def detect_anomalies(
    current_health: dict,
    prior_runs: list[dict],
    drop_threshold_pct: int,
    min_history_runs: int,
) -> list[str]:
    """Pure anomaly detection — returns list of alert message strings.

    Rules:
    - source with error set → always alert (no history needed)
    - count == 0, no error → alert with average if history exists
    - count < drop_threshold_pct % of trailing-4-run mean → drop alert
      (only when >= min_history_runs data points for that source)
    """
    alerts: list[str] = []

    for source, info in current_health.items():
        error = info.get("error")
        count = info.get("count", 0)

        if error:
            alerts.append(f"{source}: FAILED — {error}")
            continue

        # Collect trailing non-error counts for this source
        prior_counts: list[int] = []
        for run in prior_runs:
            src_info = run.get("health", {}).get(source, {})
            if not src_info.get("error") and "count" in src_info:
                prior_counts.append(src_info["count"])
        trailing = prior_counts[-4:]  # trailing 4 non-error runs

        if count == 0:
            if trailing:
                avg = sum(trailing) / len(trailing)
                alerts.append(f"{source}: 0 items (was averaging {avg:.0f})")
            else:
                alerts.append(f"{source}: 0 items")
            continue

        if len(trailing) >= min_history_runs:
            avg = sum(trailing) / len(trailing)
            if avg > 0 and count < avg * drop_threshold_pct / 100:
                alerts.append(
                    f"{source}: only {count} items (avg {avg:.0f}, "
                    f"below {drop_threshold_pct}% threshold)"
                )

    return alerts
