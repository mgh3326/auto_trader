"""Unit tests for the ROB-471 KIS overseas-price live smoke pure helpers.

The live KIS call itself is operator-run (creds-gated); here we pin the pure
decision/formatting helpers that gate the smoke's verdict and report.
"""

from __future__ import annotations

import pytest

from scripts.kis_overseas_price_smoke import (
    EXPECTED_FIELDS,
    decide_exit_code,
    evaluate_field_presence,
    missing_kis_cred_names,
)


def test_expected_fields_match_production_mapping():
    # The smoke verifies exactly the HHDFS00000300 fields the production
    # parser maps (last->close, base->previous_close, tvol->volume).
    assert EXPECTED_FIELDS == ("last", "base", "tvol")


def test_evaluate_field_presence_all_present():
    out = {"last": "205.12", "base": "201.5", "tvol": "123456"}
    assert evaluate_field_presence(out) == {
        "last": True,
        "base": True,
        "tvol": True,
    }


def test_evaluate_field_presence_treats_none_and_blank_as_absent():
    out = {"last": "", "base": None, "tvol": "5"}
    assert evaluate_field_presence(out) == {
        "last": False,
        "base": False,
        "tvol": True,
    }


def test_evaluate_field_presence_empty_output():
    assert evaluate_field_presence({}) == {
        "last": False,
        "base": False,
        "tvol": False,
    }


@pytest.mark.parametrize(
    "price,expected",
    [(205.12, 0), (0.0, 2), (-1.0, 2), (None, 2)],
)
def test_decide_exit_code(price, expected):
    assert decide_exit_code(price) == expected


def test_missing_kis_cred_names_reports_only_unset(monkeypatch):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.setenv("KIS_APP_SECRET", "present")
    assert missing_kis_cred_names() == ["KIS_APP_KEY"]


def test_missing_kis_cred_names_empty_when_all_present(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "x")
    monkeypatch.setenv("KIS_APP_SECRET", "y")
    assert missing_kis_cred_names() == []
