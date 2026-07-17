from unittest.mock import MagicMock, patch

from src.fetchers import fetch_all_sources


def _settings_all_enabled():
    s = MagicMock()
    s.source_enabled = MagicMock(return_value=True)
    return s


def test_only_sources_restricts_fetchers():
    # _FETCHERS binds function objects at import time, so patching
    # src.fetchers.soundcloud.fetch would NOT intercept — patch the registry.
    s = _settings_all_enabled()
    sc, bp = MagicMock(return_value=[]), MagicMock(return_value=[])
    with patch("src.fetchers._FETCHERS", [("soundcloud", sc), ("beatport", bp)]):
        items, health = fetch_all_sources(s, only_sources=["soundcloud"])
    sc.assert_called_once()
    bp.assert_not_called()
    assert "beatport" not in health and "soundcloud" in health


def test_bpm_ranges_forwarded_to_fetchers():
    s = _settings_all_enabled()
    sc = MagicMock(return_value=[])
    with patch("src.fetchers._FETCHERS", [("soundcloud", sc)]):
        fetch_all_sources(s, only_sources=["soundcloud"], bpm_ranges=[(170.0, 180.0)])
    assert sc.call_args.kwargs["bpm_ranges"] == [(170.0, 180.0)]
