def test_smoke_imports():
    """If src modules import cleanly, the test harness is wired up."""
    from src.models import Candidate
    from src.pipeline.ranker import rank_candidates
    from src.pipeline.report import generate_report
    from src.llm import call_stage1
    assert callable(rank_candidates)
    assert callable(generate_report)
    assert callable(call_stage1)
    assert Candidate
