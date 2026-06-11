"""Tests for src/pipeline/source_health.py."""
import pytest

from src.pipeline.source_health import (
    append_run_health,
    load_run_health,
    detect_anomalies,
    _RETENTION,
)


def _prior_run(source: str, count: int, error: str = None) -> dict:
    return {
        "report_id": "2026-W01",
        "run_at": "2026-01-01T00:00:00+00:00",
        "health": {source: {"count": count, "error": error}},
    }


# ---------------------------------------------------------------------------
# append_run_health + load_run_health
# ---------------------------------------------------------------------------

def test_append_and_load_round_trip(tmp_path):
    health = {"beatport": {"count": 50, "error": None}}
    append_run_health(health, str(tmp_path), "2026-W01")
    runs = load_run_health(str(tmp_path))
    assert len(runs) == 1
    assert runs[0]["report_id"] == "2026-W01"
    assert runs[0]["health"]["beatport"]["count"] == 50


def test_load_missing_returns_empty(tmp_path):
    assert load_run_health(str(tmp_path)) == []


def test_retention_prunes_to_26(tmp_path):
    health = {"beatport": {"count": 10, "error": None}}
    for i in range(_RETENTION + 5):
        append_run_health(health, str(tmp_path), f"2026-W{i:02d}")
    runs = load_run_health(str(tmp_path))
    assert len(runs) == _RETENTION
    # Oldest entries pruned — last entry is most recent
    assert runs[-1]["report_id"] == f"2026-W{_RETENTION + 4:02d}"


# ---------------------------------------------------------------------------
# detect_anomalies — pure
# ---------------------------------------------------------------------------

def test_error_always_alerts():
    current = {"bandcamp": {"count": 0, "error": "timeout"}}
    alerts = detect_anomalies(current, [], drop_threshold_pct=50, min_history_runs=2)
    assert any("FAILED" in a and "bandcamp" in a for a in alerts)


def test_zero_count_no_error_with_history_alerts_with_avg():
    prior = [_prior_run("beatport", 80), _prior_run("beatport", 60)]
    current = {"beatport": {"count": 0, "error": None}}
    alerts = detect_anomalies(current, prior, drop_threshold_pct=50, min_history_runs=2)
    assert len(alerts) == 1
    assert "0 items" in alerts[0]
    assert "averaging" in alerts[0]


def test_zero_count_no_history_alerts_plain():
    current = {"beatport": {"count": 0, "error": None}}
    alerts = detect_anomalies(current, [], drop_threshold_pct=50, min_history_runs=2)
    assert len(alerts) == 1
    assert "0 items" in alerts[0]
    assert "averaging" not in alerts[0]


def test_drop_below_threshold_alerts():
    prior = [
        _prior_run("beatport", 80),
        _prior_run("beatport", 80),
        _prior_run("beatport", 80),
    ]
    current = {"beatport": {"count": 10, "error": None}}  # 10 < 50% of 80
    alerts = detect_anomalies(current, prior, drop_threshold_pct=50, min_history_runs=2)
    assert len(alerts) == 1
    assert "beatport" in alerts[0]


def test_above_threshold_no_alert():
    prior = [_prior_run("beatport", 80), _prior_run("beatport", 80)]
    current = {"beatport": {"count": 50, "error": None}}  # 50 >= 50% of 80
    alerts = detect_anomalies(current, prior, drop_threshold_pct=50, min_history_runs=2)
    assert alerts == []


def test_cold_start_suppresses_drop_detection():
    """Fewer than min_history_runs → no drop alert (but error still fires)."""
    prior = [_prior_run("beatport", 80)]  # only 1 run, min is 2
    current = {"beatport": {"count": 1, "error": None}}
    alerts = detect_anomalies(current, prior, drop_threshold_pct=50, min_history_runs=2)
    assert alerts == []


def test_cold_start_still_alerts_on_error():
    """Cold start does not suppress error alerts."""
    current = {"bandcamp": {"count": 0, "error": "DNS failure"}}
    alerts = detect_anomalies(current, [], drop_threshold_pct=50, min_history_runs=2)
    assert any("FAILED" in a for a in alerts)


def test_trailing_mean_ignores_error_runs():
    """Error runs should not count toward the trailing mean for drop detection."""
    prior = [
        _prior_run("beatport", 80),
        _prior_run("beatport", 0, error="timeout"),  # error run — excluded from trailing mean
        _prior_run("beatport", 80),
    ]
    # mean of non-error runs = 80; 50% = 40; count 45 > 40 → no alert
    current = {"beatport": {"count": 45, "error": None}}
    alerts = detect_anomalies(current, prior, drop_threshold_pct=50, min_history_runs=2)
    assert alerts == []
