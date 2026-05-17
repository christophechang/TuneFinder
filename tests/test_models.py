from src.models import Candidate


def test_candidate_pool_added_at_defaults_to_none():
    c = Candidate(artist="x", title="y", link="", source="test")
    assert c.pool_added_at is None


def test_candidate_pool_added_at_accepts_iso_string():
    c = Candidate(artist="x", title="y", link="", source="test",
                  pool_added_at="2026-04-01T00:00:00+00:00")
    assert c.pool_added_at == "2026-04-01T00:00:00+00:00"
