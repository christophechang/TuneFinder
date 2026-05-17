from src.models import PoolRecord
from src.pipeline.pool import pool_to_candidates


def test_pool_to_candidates_carries_added_at():
    rec = PoolRecord(
        artist="a", title="t", link="", source="s",
        added_at="2026-04-01T00:00:00+00:00",
    )
    candidates = pool_to_candidates([rec])
    assert len(candidates) == 1
    assert candidates[0].pool_added_at == "2026-04-01T00:00:00+00:00"


def test_pool_to_candidates_handles_missing_added_at():
    rec = PoolRecord(artist="a", title="t", link="", source="s", added_at="")
    candidates = pool_to_candidates([rec])
    assert candidates[0].pool_added_at is None
