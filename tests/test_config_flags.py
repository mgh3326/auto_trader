from app.core.config import settings


def test_live_auto_reconcile_flags_default_false():
    assert settings.KIS_LIVE_AUTO_RECONCILE_ENABLED is False
    assert settings.KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False
    assert settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED is False
    assert settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False
