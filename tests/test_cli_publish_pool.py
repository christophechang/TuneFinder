"""Tests for the `publish-pool` CLI wiring (tunefinder/__main__.py).

The orchestration itself is covered by test_publisher_run.py; these tests cover
what the command adds: the target list, the flags, the exit codes and
`--write-settings`. `publish_pool` and `load_pool_settings` are patched at their
definition sites, as test_cli_mix_prep.py patches the run services.
"""
import argparse
import shutil
from unittest.mock import MagicMock, patch

import pytest

from src.publisher.pool_settings import POOL_SETTINGS_PATH
from src.publisher.run import PublishOutcome, TargetOutcome
from tunefinder.__main__ import cmd_publish_pool, main


def _args(**kw):
    return argparse.Namespace(
        env=kw.pop("env", None),
        dry_run=kw.pop("dry_run", False),
        replay=kw.pop("replay", None),
        write_settings=kw.pop("write_settings", False),
        **kw,
    )


def _settings(targets=("dev",)):
    settings = MagicMock()
    settings.pool_targets = list(targets)
    return settings


def _outcome(**kw):
    return PublishOutcome(
        run_id=kw.pop("run_id", "2026-09-06T06:00:00Z-a3f9c1"),
        started_at="2026-09-06T06:00:00Z",
        items=kw.pop("items", 10),
        batches=kw.pop("batches", 1),
        skipped_items=kw.pop("skipped_items", {}),
        per_source=kw.pop("per_source", {}),
        targets=kw.pop("targets", []),
        **kw,
    )


def _patched(args, outcome, settings=None):
    with patch("src.publisher.run.publish_pool", return_value=outcome) as mock_publish, \
         patch("src.publisher.pool_settings.load_pool_settings",
               return_value=settings or _settings()):
        try:
            cmd_publish_pool(args)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return mock_publish, code


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def test_default_env_from_pool_settings():
    mock_publish, code = _patched(_args(), _outcome(), settings=_settings(("dev",)))
    assert code == 0
    assert mock_publish.call_args[0][1].envs == ["dev"]


def test_explicit_env_wins():
    mock_publish, _ = _patched(_args(env="prod"), _outcome())
    assert mock_publish.call_args[0][1].envs == ["prod"]


def test_env_both_expands():
    mock_publish, _ = _patched(_args(env="both"), _outcome())
    assert mock_publish.call_args[0][1].envs == ["dev", "prod"]


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def test_dry_run_flag_threaded(capsys):
    mock_publish, code = _patched(_args(dry_run=True), _outcome())
    options = mock_publish.call_args[0][1]
    assert options.dry_run is True
    assert code == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_replay_flag_threaded():
    mock_publish, _ = _patched(_args(replay="2026-09-05T06:00:00Z-b1c2d3"), _outcome())
    assert mock_publish.call_args[0][1].replay_run_id == "2026-09-05T06:00:00Z-b1c2d3"


# ---------------------------------------------------------------------------
# Exit codes and output
# ---------------------------------------------------------------------------

def test_ok_prints_summary_exits_0(capsys):
    outcome = _outcome(
        targets=[TargetOutcome(env="dev", base_url="https://api-dev.example.test",
                               batches_acked=1, upserted=10, request_charge=25.0,
                               post_seconds=1.0)],
    )
    _, code = _patched(_args(), outcome)
    assert code == 0
    assert outcome.summary_line() in capsys.readouterr().out


def test_skip_exits_1_with_reason(capsys):
    outcome = _outcome(skipped=True, skip_reason="lock_held", items=0, batches=0)
    _, code = _patched(_args(), outcome)
    assert code == 1
    assert "lock_held" in capsys.readouterr().out


def test_target_error_exits_1(capsys):
    outcome = _outcome(
        targets=[TargetOutcome(env="dev", base_url="https://api-dev.example.test",
                               error="batch 2: status=503 error=transport")],
    )
    _, code = _patched(_args(), outcome)
    assert code == 1
    assert "503" in capsys.readouterr().out


def test_prints_per_source_and_skipped_item_tables(capsys):
    outcome = _outcome(
        items=3,
        skipped_items={"no_fine_genre": 57, "no_source_ref": 3},
        per_source={
            "beatport": {"count": 1200, "error": None, "enabled": True},
            "soundcloud": {"count": 0, "error": None, "enabled": False},
            "volumo": {"count": 0, "error": "connection reset", "enabled": True},
        },
    )
    _, _ = _patched(_args(), outcome)
    out = capsys.readouterr().out
    assert "beatport" in out and "1200" in out
    assert "disabled" in out
    assert "connection reset" in out
    assert "no_fine_genre" in out and "57" in out


def test_lock_held_error_exits_1_cleanly(capsys):
    from src.pipeline.storage import RunLockHeldError

    with patch("src.publisher.run.publish_pool",
               side_effect=RunLockHeldError("another TuneFinder run is in progress")), \
         patch("src.publisher.pool_settings.load_pool_settings", return_value=_settings()):
        with pytest.raises(SystemExit) as exc_info:
            cmd_publish_pool(_args())
    assert exc_info.value.code == 1
    assert "another TuneFinder run is in progress" in capsys.readouterr().out


def test_settings_validate_is_not_called():
    """The publisher runs without Discord credentials; alerts degrade instead."""
    settings = _settings()
    _patched(_args(), _outcome(), settings=settings)
    settings.validate.assert_not_called()


# ---------------------------------------------------------------------------
# --write-settings
# ---------------------------------------------------------------------------

def test_write_settings_regenerates_and_exits(tmp_path, capsys):
    target = tmp_path / "settings.pool.yaml"
    shutil.copyfile(POOL_SETTINGS_PATH, target)

    with patch("src.publisher.pool_settings.POOL_SETTINGS_PATH", str(target)), \
         patch("src.publisher.run.publish_pool") as mock_publish:
        cmd_publish_pool(_args(write_settings=True))
        out = capsys.readouterr().out
        assert "unchanged" in out
        assert str(target) in out

        target.write_text("# stale\n")
        cmd_publish_pool(_args(write_settings=True))
        assert "updated" in capsys.readouterr().out

    mock_publish.assert_not_called()
    assert target.read_bytes() == open(POOL_SETTINGS_PATH, "rb").read()


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def test_argparse_accepts_every_publish_pool_flag():
    with patch("tunefinder.__main__.cmd_publish_pool") as mock_cmd, \
         patch("sys.argv", ["tunefinder", "publish-pool", "--env", "both", "--dry-run",
                            "--replay", "2026-09-05T06:00:00Z-b1c2d3"]):
        main()
    args = mock_cmd.call_args[0][0]
    assert args.env == "both"
    assert args.dry_run is True
    assert args.replay == "2026-09-05T06:00:00Z-b1c2d3"
    assert args.write_settings is False


def test_argparse_write_settings_flag():
    with patch("tunefinder.__main__.cmd_publish_pool") as mock_cmd, \
         patch("sys.argv", ["tunefinder", "publish-pool", "--write-settings"]):
        main()
    assert mock_cmd.call_args[0][0].write_settings is True
