"""Unit tests for the ROB-922 KIS overseas-premarket probe pure helpers.

The live KIS/Yahoo calls are operator-run (creds-gated, network); here we pin
the pure decision/formatting helpers the probe uses to build its report.
"""

from __future__ import annotations

import pytest

from scripts.kis_overseas_premarket_probe import (
    _to_float,
    missing_kis_cred_names,
)


def test_missing_kis_cred_names_reports_only_unset(monkeypatch):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.setenv("KIS_APP_SECRET", "present")
    assert missing_kis_cred_names() == ["KIS_APP_KEY"]


def test_missing_kis_cred_names_empty_when_all_present(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "x")
    monkeypatch.setenv("KIS_APP_SECRET", "y")
    assert missing_kis_cred_names() == []


def test_missing_kis_cred_names_reports_both_when_unset(monkeypatch):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    assert missing_kis_cred_names() == ["KIS_APP_KEY", "KIS_APP_SECRET"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("205.12", 205.12),
        (205.12, 205.12),
        (None, None),
        ("", None),
        ("not-a-number", None),
    ],
)
def test_to_float(value, expected):
    assert _to_float(value) == expected
