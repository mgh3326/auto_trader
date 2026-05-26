import pytest

from scripts.smoke import us_dual_paper_preview_smoke as smoke


@pytest.mark.unit
def test_disabled_is_noop_exit_zero(monkeypatch, capsys):
    monkeypatch.delenv("US_DUAL_PAPER_PREVIEW_ENABLED", raising=False)
    rc = smoke.main(["--mode", "preflight"])
    assert rc == 0
    assert "US_DUAL_PAPER_PREVIEW_ENABLED" in capsys.readouterr().out


@pytest.mark.unit
def test_preflight_reports_missing_env_names_only(monkeypatch, capsys):
    monkeypatch.setenv("US_DUAL_PAPER_PREVIEW_ENABLED", "true")
    # Force both brokers to look disabled by clearing creds
    for key in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_API_SECRET",
                "KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET", "KIS_MOCK_ACCOUNT_NO"):
        monkeypatch.delenv(key, raising=False)
    rc = smoke.main(["--mode", "preflight"])
    out = capsys.readouterr().out
    # exit 1 = config/credential problem; names present, no secret values
    assert rc in (0, 1)
    assert "missing_env_keys" in out
