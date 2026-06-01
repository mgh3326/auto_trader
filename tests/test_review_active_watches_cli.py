"""ROB-337 Slice 2 — review_active_watches CLI gate/dry-run."""

from __future__ import annotations

from scripts.review_active_watches import main


def test_disabled_without_env(monkeypatch, capsys) -> None:
    monkeypatch.delenv("WATCH_VALIDITY_REVIEW_ENABLED", raising=False)
    assert main(["--dry-run"]) == 0
    assert "disabled" in capsys.readouterr().out.lower()


def test_dry_run_when_enabled(monkeypatch, capsys) -> None:
    monkeypatch.setenv("WATCH_VALIDITY_REVIEW_ENABLED", "true")
    assert main(["--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out.lower()
