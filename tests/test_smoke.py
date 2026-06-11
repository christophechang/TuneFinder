def test_smoke_imports():
    """If src modules import cleanly, the test harness is wired up."""
    from src.models import Candidate
    from src.pipeline.ranker import rank_candidates
    from src.pipeline.report import generate_report
    from src.pipeline.reasons import compose_reason
    assert callable(rank_candidates)
    assert callable(generate_report)
    assert callable(compose_reason)
    assert Candidate
