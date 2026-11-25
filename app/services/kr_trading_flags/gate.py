"""hk 6503 — pure screener gate over the stored KR trading flags.

Exclusion, per the closed contract:

* 관리 (management ``designated``)            -> excluded
* 투자경고 / 투자위험 (market_alert warning/risk) -> excluded
* 정리매매 (delisting_trade ``in_progress``)     -> excluded
* 거래정지 (trading_halt ``halted``)             -> excluded

Labelled, not excluded:

* 투자주의 (market_alert ``caution``)           -> ``trading_flag_labels`` on the row

Unknown (no stored row) and unmapped never exclude. They are not evidence that a
flag is set. The gate keeps the row and reports nothing about it.

Every exclusion carries its evidence (category, state, source, observed time,
source reason code, effective date) so an operator can see and overrule it.

Pure: no DB, no clock, no network. The caller supplies the records.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.store import FlagRecord

#: Stable reason code per excluded category (the operator-facing label is Korean).
EXCLUSION_REASON_CODE: dict[tuple[str, str], str] = {
    (v.CATEGORY_MANAGEMENT, "designated"): "management_designated",
    (v.CATEGORY_MARKET_ALERT, "warning"): "market_alert_warning",
    (v.CATEGORY_MARKET_ALERT, "risk"): "market_alert_risk",
    (v.CATEGORY_DELISTING_TRADE, "in_progress"): "delisting_trade_in_progress",
    (v.CATEGORY_TRADING_HALT, "halted"): "trading_halt",
}


@dataclass
class GateOutcome:
    kept: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    labelled_rows: int = 0


def evidence_of(record: FlagRecord) -> dict[str, Any]:
    return {
        "category": record.category,
        "state": record.state,
        "label_ko": v.STATE_LABEL_KO.get((record.category, record.state)),
        "raw_code": record.raw_code,
        "reason_code": record.reason_code,
        "effective_date": (
            record.effective_date.isoformat() if record.effective_date else None
        ),
        "source": record.source,
        "observed_at": record.observed_at.isoformat(),
    }


def apply_trading_flag_gate(
    rows: list[Mapping[str, Any]],
    *,
    symbol_of: Callable[[Mapping[str, Any]], str | None],
    records: Mapping[str, Mapping[str, FlagRecord]],
) -> GateOutcome:
    """Split ``rows`` into kept and excluded, and label 투자주의 rows.

    ``records`` is ``{symbol: {category: FlagRecord}}``. A symbol or category
    missing from it is unknown and does not exclude.
    """
    outcome = GateOutcome()
    for row in rows:
        symbol = symbol_of(row)
        flags = records.get(symbol, {}) if symbol else {}

        reasons = []
        for category, record in sorted(flags.items()):
            if v.is_excluded(category, record.state):
                reasons.append(
                    {
                        "reason": EXCLUSION_REASON_CODE[(category, record.state)],
                        **evidence_of(record),
                    }
                )
        if reasons:
            outcome.excluded.append({"symbol": symbol, "reasons": reasons})
            continue

        labels = [
            evidence_of(record)
            for category, record in sorted(flags.items())
            if category == v.CATEGORY_MARKET_ALERT and record.state == "caution"
        ]
        if labels:
            outcome.labelled_rows += 1
            outcome.kept.append({**row, "trading_flag_labels": labels})
        else:
            outcome.kept.append(dict(row))
    return outcome
