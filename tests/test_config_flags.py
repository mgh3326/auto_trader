from app.core.config import settings


def test_kis_live_auto_reconcile_flag_defaults_false():
    assert settings.KIS_LIVE_AUTO_RECONCILE_ENABLED is False
