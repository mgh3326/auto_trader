import pytest

from scripts.invest_reports_us_schedule import main


@pytest.mark.unit
def test_disabled_is_noop_exit_zero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("INVEST_REPORTS_US_SCHEDULE_ENABLED", raising=False)
    rc = main(["--run"])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out.lower()


@pytest.mark.unit
def test_help_works_without_env(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


@pytest.mark.unit
def test_dry_run_prints_plan_without_side_effects(monkeypatch, capsys) -> None:
    monkeypatch.setenv("INVEST_REPORTS_US_SCHEDULE_ENABLED", "true")
    rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "prepare_bundle" in out
    assert "kis_live" in out and "advisory_only" in out
    assert "kis_mock" in out and "mock_preview" in out


@pytest.mark.unit
def test_enabled_no_mode_prints_guidance(monkeypatch, capsys) -> None:
    monkeypatch.setenv("INVEST_REPORTS_US_SCHEDULE_ENABLED", "true")
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "no action" in out or "--dry-run" in out
