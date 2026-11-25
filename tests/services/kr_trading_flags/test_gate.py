"""hk 6503: screener gate rules (pure, no database)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.gate import apply_trading_flag_gate
from app.services.kr_trading_flags.store import FlagRecord

pytestmark = pytest.mark.unit

OBSERVED = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)


def _rec(
    category: str,
    state: str,
    *,
    raw: str = "",
    reason: str | None = None,
    on: date | None = None,
) -> FlagRecord:
    return FlagRecord(
        category=category,
        state=state,
        raw_code=raw or None,
        reason_code=reason,
        effective_date=on,
        source=v.SOURCE_NAVER_INTRADAY,
        observed_at=OBSERVED,
    )


def _sym(row: dict[str, Any]) -> str | None:
    return row.get("code")


def _rows(*codes: str) -> list[dict[str, Any]]:
    return [{"code": code, "name": f"n{code}"} for code in codes]


def test_management_designated_is_excluded_with_evidence_reason_code_and_date() -> None:
    """Acceptance 1 & 2: 더코디(224060) 관리 is excluded, reason and date reported."""
    records = {
        "224060": {
            v.CATEGORY_MANAGEMENT: _rec(
                v.CATEGORY_MANAGEMENT,
                "designated",
                raw="1",
                reason="6045",
                on=date(2026, 11, 25),
            ),
            v.CATEGORY_MARKET_ALERT: _rec(v.CATEGORY_MARKET_ALERT, "caution", raw="01"),
        }
    }
    out = apply_trading_flag_gate(_rows("224060"), symbol_of=_sym, records=records)
    assert out.kept == []
    (excluded,) = out.excluded
    assert excluded["symbol"] == "224060"
    (reason,) = excluded["reasons"]
    assert reason["reason"] == "management_designated"
    assert reason["label_ko"] == "관리"
    assert reason["reason_code"] == "6045"
    assert reason["effective_date"] == "2026-11-25"
    assert reason["source"] == v.SOURCE_NAVER_INTRADAY


def test_caution_is_labelled_and_kept_not_excluded() -> None:
    """Acceptance 2: 투자주의 is a label only."""
    records = {
        "005930": {
            v.CATEGORY_MARKET_ALERT: _rec(v.CATEGORY_MARKET_ALERT, "caution", raw="01")
        }
    }
    out = apply_trading_flag_gate(_rows("005930"), symbol_of=_sym, records=records)
    assert out.excluded == []
    assert out.labelled_rows == 1
    (row,) = out.kept
    assert row["trading_flag_labels"][0]["label_ko"] == "투자주의"


@pytest.mark.parametrize(
    ("category", "state", "reason_code"),
    [
        (v.CATEGORY_MARKET_ALERT, "warning", "market_alert_warning"),
        (v.CATEGORY_MARKET_ALERT, "risk", "market_alert_risk"),
        (v.CATEGORY_DELISTING_TRADE, "in_progress", "delisting_trade_in_progress"),
        (v.CATEGORY_TRADING_HALT, "halted", "trading_halt"),
    ],
)
def test_warning_risk_delisting_and_halt_are_excluded(
    category: str, state: str, reason_code: str
) -> None:
    records = {"111111": {category: _rec(category, state)}}
    out = apply_trading_flag_gate(_rows("111111"), symbol_of=_sym, records=records)
    assert out.kept == []
    assert out.excluded[0]["reasons"][0]["reason"] == reason_code


def test_missing_data_is_unknown_never_false() -> None:
    """Acceptance 3: no stored row keeps the row, labels nothing, excludes nothing."""
    out = apply_trading_flag_gate(_rows("222222", "333333"), symbol_of=_sym, records={})
    assert out.excluded == []
    assert out.labelled_rows == 0
    assert [row["code"] for row in out.kept] == ["222222", "333333"]
    assert all("trading_flag_labels" not in row for row in out.kept)


def test_unmapped_and_none_never_exclude() -> None:
    records = {
        "444444": {
            v.CATEGORY_MANAGEMENT: _rec(
                v.CATEGORY_MANAGEMENT, v.STATE_UNMAPPED, raw="3"
            ),
            v.CATEGORY_TRADING_HALT: _rec(v.CATEGORY_TRADING_HALT, "none", raw="N"),
        }
    }
    out = apply_trading_flag_gate(_rows("444444"), symbol_of=_sym, records=records)
    assert out.excluded == []
    assert len(out.kept) == 1


def test_input_rows_are_not_mutated() -> None:
    rows = _rows("555555")
    records = {
        "555555": {v.CATEGORY_MARKET_ALERT: _rec(v.CATEGORY_MARKET_ALERT, "caution")}
    }
    apply_trading_flag_gate(rows, symbol_of=_sym, records=records)
    assert "trading_flag_labels" not in rows[0]
