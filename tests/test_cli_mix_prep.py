"""Tests for the mix-prep CLI's BPM/key argument parsing and validation
(issue #8: `mix-prep <genre> --bpm MIN-MAX [--key CODE] [--no-bpm-flex]`)."""
import argparse
from unittest.mock import MagicMock, patch

import pytest

from tunefinder.__main__ import _parse_bpm_range, cmd_mix_prep, main


# ---------------------------------------------------------------------------
# _parse_bpm_range
# ---------------------------------------------------------------------------

def test_parse_bpm_range_valid():
    assert _parse_bpm_range("170-180") == (170.0, 180.0)


def test_parse_bpm_range_decimal():
    assert _parse_bpm_range("140.5-150.5") == (140.5, 150.5)


def test_parse_bpm_range_tolerates_surrounding_whitespace():
    assert _parse_bpm_range(" 170 - 180 ") == (170.0, 180.0)


def test_parse_bpm_range_equal_bounds_ok():
    assert _parse_bpm_range("174-174") == (174.0, 174.0)


@pytest.mark.parametrize("raw", ["180-170", "180.5-170"])
def test_parse_bpm_range_min_greater_than_max_raises(raw):
    with pytest.raises(ValueError, match="min .* must be <= max"):
        _parse_bpm_range(raw)


@pytest.mark.parametrize("raw", ["170", "170,180", "abc-def", "", "170--180", "170 to 180"])
def test_parse_bpm_range_invalid_format_raises(raw):
    with pytest.raises(ValueError, match="invalid --bpm value"):
        _parse_bpm_range(raw)


# ---------------------------------------------------------------------------
# cmd_mix_prep — clean CLI-level error handling (fails before any fetching)
# ---------------------------------------------------------------------------

def test_cmd_mix_prep_bad_bpm_exits_cleanly(capsys):
    args = argparse.Namespace(genre="dnb", dry_run=True, bpm="not-a-range", key=None, no_bpm_flex=False)
    with patch("tunefinder.__main__.load_settings") as mock_load_settings:
        with pytest.raises(SystemExit) as exc_info:
            cmd_mix_prep(args)
        mock_load_settings.assert_not_called()
    assert exc_info.value.code == 1
    assert "invalid --bpm value" in capsys.readouterr().out


def test_cmd_mix_prep_bad_bpm_range_exits_cleanly(capsys):
    args = argparse.Namespace(genre="dnb", dry_run=True, bpm="180-170", key=None, no_bpm_flex=False)
    with pytest.raises(SystemExit) as exc_info:
        cmd_mix_prep(args)
    assert exc_info.value.code == 1
    assert "must be <= max" in capsys.readouterr().out


def test_cmd_mix_prep_bad_key_exits_cleanly(capsys):
    args = argparse.Namespace(genre="dnb", dry_run=True, bpm=None, key="notakey", no_bpm_flex=False)
    with patch("tunefinder.__main__.load_settings") as mock_load_settings:
        with pytest.raises(SystemExit) as exc_info:
            cmd_mix_prep(args)
        mock_load_settings.assert_not_called()
    assert exc_info.value.code == 1
    assert "could not parse --key" in capsys.readouterr().out


def test_cmd_mix_prep_valid_key_musical_notation_accepted():
    """A musical-notation --key (not just Camelot) must not raise before
    load_settings runs — proves to_camelot's normalisation is used for
    validation, matching the task spec ('accept Camelot or musical notation')."""
    args = argparse.Namespace(genre="dnb", dry_run=True, bpm=None, key="Am", no_bpm_flex=False)
    with patch("tunefinder.__main__.load_settings", side_effect=RuntimeError("stop-here")):
        with pytest.raises(RuntimeError, match="stop-here"):
            cmd_mix_prep(args)


# ---------------------------------------------------------------------------
# argparse wiring — the real parser accepts and threads --bpm/--key/--no-bpm-flex
# ---------------------------------------------------------------------------

def test_argparse_accepts_bpm_key_no_bpm_flex_flags():
    with patch("tunefinder.__main__.cmd_mix_prep") as mock_cmd, \
         patch("tunefinder.__main__.setup_logging"), \
         patch("sys.argv", ["tunefinder", "mix-prep", "dnb", "--bpm", "170-180",
                            "--key", "8A", "--no-bpm-flex"]):
        main()
    mock_cmd.assert_called_once()
    called_args = mock_cmd.call_args[0][0]
    assert called_args.genre == "dnb"
    assert called_args.bpm == "170-180"
    assert called_args.key == "8A"
    assert called_args.no_bpm_flex is True


def test_argparse_bpm_key_default_to_none_and_flex_default_false_flag():
    with patch("tunefinder.__main__.cmd_mix_prep") as mock_cmd, \
         patch("tunefinder.__main__.setup_logging"), \
         patch("sys.argv", ["tunefinder", "mix-prep", "dnb"]):
        main()
    called_args = mock_cmd.call_args[0][0]
    assert called_args.bpm is None
    assert called_args.key is None
    assert called_args.no_bpm_flex is False
