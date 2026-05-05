from app.core.config import settings

def test_research_pipeline_flags_defaults():
    """Assert that the research pipeline feature flags exist and default to False."""
    assert hasattr(settings, "RESEARCH_PIPELINE_ENABLED")
    assert hasattr(settings, "RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED")
    assert hasattr(settings, "RESEARCH_PIPELINE_DUAL_WRITE_ENABLED")
    
    assert settings.RESEARCH_PIPELINE_ENABLED is False
    assert settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED is False
    assert settings.RESEARCH_PIPELINE_DUAL_WRITE_ENABLED is False
