"""Tests for the `tunefinder free-downloads <genre>` CLI command — mirrors
tests/test_cli_mix_prep.py's patching pattern for the shared free_only run path."""
from unittest.mock import MagicMock, patch

import pytest

import tunefinder.__main__ as cli
from src.services.runs import RunOutcome


def _outcome():
    return RunOutcome(kind="free-downloads", report_id="2026-W29-free-dl-dnb",
                      dry_run=True, recommended_count=3, duration_seconds=1)


def test_cli_free_downloads_invokes_free_only_options(capsys):
    settings = MagicMock()
    with patch.object(cli, "load_settings", return_value=settings), \
         patch("src.services.runs.run_mix_prep", return_value=_outcome()) as mock_run, \
         patch("sys.argv", ["tunefinder", "free-downloads", "dnb", "--bpm", "170-180", "--dry-run"]):
        cli.main()

    options = mock_run.call_args.args[1]
    assert options.free_only is True
    assert options.genre == "dnb"
    assert options.bpm_range == (170.0, 180.0)
    assert options.dry_run is True
    assert "free-dl" in capsys.readouterr().out


def test_cli_free_downloads_rejects_unknown_genre():
    with patch("sys.argv", ["tunefinder", "free-downloads", "polka"]):
        with pytest.raises(SystemExit):
            cli.main()
