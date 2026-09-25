"""Tests for the Volumo preview check (src/publisher/previews.py).

Offline: `head` is a fake responder keyed by track id, and `clock`/`sleep`
are the same deterministic pair the run tests use.
"""
from datetime import datetime, timedelta, timezone

import requests

from src.publisher.previews import (
    VOLUMO_PREVIEW_TOKEN,
    check_volumo_previews,
    load_preview_cache,
    prune_preview_cache,
    save_preview_cache,
)

START = datetime(2026, 9, 26, 5, 0, 0, tzinfo=timezone.utc)
SEEN_AT = "2026-09-26T05:00:00Z"


class Clock:
    def __init__(self, start=START):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


class FakeHead:
    """Answers by track id; an exception instance is raised, anything else is
    the status code. Unlisted ids answer 200."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> int:
        self.urls.append(url)
        track_id = url.split("/tracks/")[1].split("/")[0]
        answer = self.answers.get(track_id, 200)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _volumo(track_id, families=("house",)):
    return {
        "key_v2": f"k-{track_id}",
        "families": list(families),
        "preview": {
            "kind": "volumo_prelisten",
            "ref": f"https://volumo.com/api/v1/tracks/{track_id}/prelisten.mp3",
            "eligible": True,
            "checked_at": SEEN_AT,
        },
    }


def _beatport(n=1):
    return {
        "key_v2": f"bp-{n}",
        "families": ["techno"],
        "preview": {
            "kind": "beatport_sample",
            "ref": f"https://geo-samples.beatport.com/track/{n}.LOFI.mp3",
            "eligible": True,
            "checked_at": SEEN_AT,
        },
    }


def _check(items, head, clock=None, cache=None, **kw):
    clock = clock or Clock()
    cache = {} if cache is None else cache
    result = check_volumo_previews(
        items, cache, head=head, clock=clock, sleep=clock.sleep, **kw
    )
    return result, cache, clock


def test_200_is_eligible_and_checked_at_is_the_check_time():
    clock = Clock()
    clock.now = START + timedelta(minutes=3)
    item = _volumo("101")

    result, cache, _ = _check([item], FakeHead({"101": 200}), clock=clock)

    assert item["preview"]["eligible"] is True
    assert item["preview"]["checked_at"] == "2026-09-26T05:03:00Z"
    assert item["preview"]["ref"] == "https://volumo.com/api/v1/tracks/101/prelisten.mp3"
    assert result.checked == 1
    assert cache["101"]["eligible"] is True


def test_head_url_carries_the_pinned_c_token():
    head = FakeHead()
    _check([_volumo("101")], head)
    assert head.urls == [
        f"https://volumo.com/api/v1/tracks/101/prelisten.mp3?c={VOLUMO_PREVIEW_TOKEN}"
    ]


def test_402_404_and_other_4xx_are_not_eligible():
    items = [_volumo("1"), _volumo("2"), _volumo("3")]
    result, _, _ = _check(items, FakeHead({"1": 402, "2": 404, "3": 410}))
    assert [i["preview"]["eligible"] for i in items] == [False, False, False]
    assert result.by_status == {"402": 1, "404": 1, "410": 1}


def test_timeout_and_5xx_are_not_eligible_this_run():
    items = [_volumo("1"), _volumo("2")]
    head = FakeHead({"1": requests.Timeout("slow"), "2": 503})

    result, cache, _ = _check(items, head)

    assert [i["preview"]["eligible"] for i in items] == [False, False]
    assert result.by_status == {"error": 1, "503": 1}
    # Recorded as provisional, so the next run checks them again first.
    assert cache["1"]["status"] == "error"
    assert cache["2"]["status"] == "503"


def test_beatport_sample_is_not_checked_or_changed():
    item = _beatport()
    before = dict(item["preview"])
    head = FakeHead()

    result, _, _ = _check([item], head)

    assert head.urls == []
    assert item["preview"] == before
    assert result.checked == 0


def test_items_without_a_preview_are_ignored():
    head = FakeHead()
    _check([{"key_v2": "x", "families": ["house"], "preview": None}], head)
    assert head.urls == []


def test_cap_is_honoured_and_the_remainder_stays_eligible():
    items = [_volumo(str(n)) for n in range(5)]
    head = FakeHead({str(n): 402 for n in range(5)})

    result, cache, _ = _check(items, head, cap=3)

    assert len(head.urls) == 3
    assert [i["preview"]["eligible"] for i in items] == [False, False, False, True, True]
    assert [i["preview"]["checked_at"] for i in items[3:]] == [SEEN_AT, SEEN_AT]
    assert result.checked == 3
    assert result.unchecked == 2
    assert set(cache) == {"0", "1", "2"}


def test_requests_are_sequential_with_a_delay_between_them():
    clock = Clock()
    _check([_volumo(str(n)) for n in range(3)], FakeHead(), clock=clock, delay=0.25)
    assert clock.sleeps == [0.25, 0.25]


def test_a_fresh_cached_verdict_is_reused_without_a_request():
    clock = Clock()
    cache = {"101": {"eligible": False, "status": "402", "checked_at": "2026-09-24T05:00:00Z"}}
    item = _volumo("101")
    head = FakeHead()

    result, _, _ = _check([item], head, clock=clock, cache=cache)

    assert head.urls == []
    assert item["preview"]["eligible"] is False
    assert item["preview"]["checked_at"] == "2026-09-24T05:00:00Z"
    assert result.cached == 1


def test_a_stale_cached_verdict_is_checked_again():
    cache = {"101": {"eligible": False, "status": "402", "checked_at": "2026-09-18T05:00:00Z"}}
    item = _volumo("101")

    result, cache, _ = _check([item], FakeHead({"101": 200}), cache=cache)

    assert item["preview"]["eligible"] is True
    assert cache["101"]["status"] == "200"
    assert result.checked == 1


def test_a_provisional_verdict_is_checked_again_next_run():
    cache = {"101": {"eligible": False, "status": "error", "checked_at": "2026-09-26T04:00:00Z"}}
    item = _volumo("101")
    _check([item], FakeHead({"101": 200}), cache=cache)
    assert item["preview"]["eligible"] is True


def test_never_checked_ids_go_first_then_the_oldest_stale_ones():
    cache = {
        "old": {"eligible": True, "status": "200", "checked_at": "2026-09-01T05:00:00Z"},
        "older": {"eligible": True, "status": "200", "checked_at": "2026-08-20T05:00:00Z"},
    }
    head = FakeHead()
    _check([_volumo("old"), _volumo("older"), _volumo("new")], head, cache=cache, cap=2)
    assert [u.split("/tracks/")[1].split("/")[0] for u in head.urls] == ["new", "older"]


def test_over_the_cap_a_stale_cached_verdict_is_used():
    cache = {"101": {"eligible": False, "status": "402", "checked_at": "2026-09-01T05:00:00Z"}}
    item = _volumo("101")
    head = FakeHead()

    result, _, _ = _check([_volumo("999"), item], head, cache=cache, cap=1)

    assert item["preview"]["eligible"] is False
    assert result.cached == 1


def test_one_track_on_two_items_is_checked_once():
    items = [_volumo("101", ["house"]), _volumo("101", ["techno"])]
    head = FakeHead({"101": 402})

    _check(items, head)

    assert len(head.urls) == 1
    assert [i["preview"]["eligible"] for i in items] == [False, False]


def test_five_consecutive_errors_stop_the_check():
    items = [_volumo(str(n)) for n in range(8)]
    head = FakeHead({str(n): requests.ConnectionError("down") for n in range(8)})

    result, _, _ = _check(items, head)

    assert len(head.urls) == 5
    assert result.stopped is not None
    assert [i["preview"]["eligible"] for i in items[:5]] == [False] * 5
    assert [i["preview"]["eligible"] for i in items[5:]] == [True] * 3
    assert result.unchecked == 3


def test_a_success_resets_the_error_run():
    answers = {"0": 503, "1": 503, "2": 503, "3": 503, "4": 200,
               "5": 503, "6": 503, "7": 503, "8": 503, "9": 200}
    head = FakeHead(answers)
    result, _, _ = _check([_volumo(str(n)) for n in range(10)], head)
    assert len(head.urls) == 10
    assert result.stopped is None


def test_a_single_400_is_a_verdict_on_the_track_not_a_token_refusal():
    items = [_volumo("1"), _volumo("2"), _volumo("3")]
    result, cache, _ = _check(items, FakeHead({"2": 400}))
    assert [i["preview"]["eligible"] for i in items] == [True, False, True]
    assert result.token_rejected is False
    assert result.stopped is None
    assert cache["2"]["status"] == "400"


def test_five_400s_in_a_row_are_a_token_refusal_and_stop_the_check():
    items = [_volumo(str(n)) for n in range(8)]
    head = FakeHead({str(n): 400 for n in range(8)})

    result, cache, _ = _check(items, head)

    assert len(head.urls) == 5
    assert result.token_rejected is True
    assert result.stopped is not None
    # Provisional, so a fixed token re-checks them first.
    assert {cache[str(n)]["status"] for n in range(5)} == {"token"}
    assert [i["preview"]["eligible"] for i in items[5:]] == [True] * 3
    assert result.by_family == {
        "house": {"eligible": 0, "ineligible": 0, "error": 5, "unchecked": 3}
    }


def test_request_level_4xx_are_provisional_and_trip_the_breaker():
    items = [_volumo(str(n)) for n in range(6)]
    head = FakeHead({"0": 429, "1": 403, "2": 405, "3": 408, "4": 429, "5": 200})

    result, cache, _ = _check(items, head)

    assert len(head.urls) == 5
    assert result.stopped is not None
    assert all(cache[str(n)]["eligible"] is False for n in range(5))
    assert result.by_family["house"]["error"] == 5


def test_a_provisional_verdict_over_the_cap_is_not_reused():
    cache = {"101": {"eligible": False, "status": "error", "checked_at": "2026-09-25T05:00:00Z"}}
    item = _volumo("101")

    result, _, _ = _check([_volumo("999"), item], FakeHead(), cache=cache, cap=1)

    assert item["preview"]["eligible"] is True
    assert item["preview"]["checked_at"] == SEEN_AT
    assert result.unchecked == 1
    assert result.cached == 0


def test_a_verdict_a_few_minutes_short_of_seven_days_is_due():
    cache = {"101": {"eligible": True, "status": "200", "checked_at": "2026-09-19T05:08:00Z"}}
    head = FakeHead({"101": 402})
    _check([_volumo("101")], head, cache=cache)
    assert len(head.urls) == 1


def test_the_time_budget_stops_the_check():
    clock = Clock()

    def slow_head(url):
        clock.now = clock.now + timedelta(minutes=8)
        return 200

    items = [_volumo(str(n)) for n in range(5)]
    result, _, _ = _check(items, slow_head, clock=clock)

    assert result.checked == 3
    assert result.unchecked == 2
    assert result.stopped == "time budget spent"


def test_malformed_cache_entries_are_dropped_on_load(tmp_path):
    (tmp_path / "volumo_previews.json").write_text(
        '{"a": "402", "b": {"status": "402", "checked_at": "2026-09-25T05:00:00Z"},'
        ' "c": {"eligible": false, "status": "402", "checked_at": "yesterday"},'
        ' "d": {"eligible": false, "status": "402", "checked_at": "2026-09-25T05:00:00Z"}}'
    )
    assert set(load_preview_cache(str(tmp_path))) == {"d"}


def test_counts_per_family():
    items = [
        _volumo("1", ["house"]),
        _volumo("2", ["house"]),
        _volumo("3", ["drum-and-bass"]),
        _volumo("4", ["house", "uk-garage"]),
        _beatport(),
    ]
    result, _, _ = _check(items, FakeHead({"2": 402, "3": 402}))
    assert result.by_family == {
        "drum-and-bass": {"eligible": 0, "ineligible": 1, "error": 0, "unchecked": 0},
        "house": {"eligible": 2, "ineligible": 1, "error": 0, "unchecked": 0},
        "uk-garage": {"eligible": 1, "ineligible": 0, "error": 0, "unchecked": 0},
    }


def test_family_counts_keep_the_unchecked_apart_from_the_eligible():
    items = [_volumo("1", ["house"]), _volumo("2", ["house"]), _volumo("3", ["house"])]
    cache = {"3": {"eligible": False, "status": "402", "checked_at": "2026-09-25T05:00:00Z"}}
    result, _, _ = _check(items, FakeHead(), cache=cache, cap=1)
    assert result.by_family == {
        "house": {"eligible": 1, "ineligible": 1, "error": 0, "unchecked": 1}
    }


def test_family_counts_keep_network_errors_apart_from_ineligible():
    items = [_volumo("1"), _volumo("2")]
    result, _, _ = _check(items, FakeHead({"1": 402, "2": requests.Timeout("slow")}))
    assert result.by_family == {
        "house": {"eligible": 0, "ineligible": 1, "error": 1, "unchecked": 0}
    }


def test_an_unexpected_exception_from_head_is_an_error_not_a_crash():
    item = _volumo("101")
    result, _, _ = _check([item], FakeHead({"101": ValueError("odd")}))
    assert item["preview"]["eligible"] is False
    assert result.by_status == {"error": 1}


def test_cache_round_trips_and_prunes_entries_older_than_45_days(tmp_path):
    cache = {
        "keep": {"eligible": True, "status": "200", "checked_at": "2026-09-01T05:00:00Z"},
        "drop": {"eligible": True, "status": "200", "checked_at": "2026-08-01T05:00:00Z"},
    }
    save_preview_cache(str(tmp_path), prune_preview_cache(cache, START))
    assert set(load_preview_cache(str(tmp_path))) == {"keep"}


def test_a_missing_or_corrupt_cache_loads_empty(tmp_path):
    assert load_preview_cache(str(tmp_path)) == {}
    (tmp_path / "volumo_previews.json").write_text("{not json")
    assert load_preview_cache(str(tmp_path)) == {}
