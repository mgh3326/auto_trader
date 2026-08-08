"""MAX_TABLE_AGE — B0-X experiment contract v1.1 §2-2 **literal** (not invented).

Source of record
----------------
File: ``~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md``
Contract sha256 (orch-measured 2026-08-08)::

    97278b0e8b8000e2e663c936328686001af5850087897270bc80a95ebf8f6b2e

§2-2 (verbatim, wrapped)::

    표가 없거나 STALE 이거나 MAX_TABLE_AGE 초과면 그 사이클은 주문 0
    (조용한 재사용·재계산 금지, 사유 기록).
    MAX_TABLE_AGE (v1.1, 운영자 확정 2026-08-08): crypto 8h · KR 36h · US 36h
    — X-C 검증 발 안전장치의 계약 승격, 3시장 공통 적용.

Behavior when exceeded (order-cycle consumer, not this table generator):
**that cycle emits zero orders**, records reason ``stale_by_age``, and must not
quietly reuse or recompute the table.

These hours are **not** worker-chosen. Do not change them without a contract
revision that updates the sha256 above.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

#: Contract citation stamped into every artifact / consumer path that uses the ages.
CONTRACT_V11_SECTION_2_2: Final[str] = (
    "b0x-experiment-contract-v1 §2-2 (v1.1, operator-confirmed 2026-08-08); "
    "sha256:97278b0e8b8000e2e663c936328686001af5850087897270bc80a95ebf8f6b2e"
)

#: Hours by market — contract v1.1 §2-2 literal.
MAX_TABLE_AGE_HOURS: Final[dict[str, int]] = {
    "crypto": 8,
    "kr": 36,
    "us": 36,
}

#: Timedelta form used by the B0-X order-cycle table gate.
MAX_TABLE_AGE: Final[dict[str, dt.timedelta]] = {
    market: dt.timedelta(hours=hours) for market, hours in MAX_TABLE_AGE_HOURS.items()
}


def max_table_age_stamp(market: str) -> dict[str, str | int]:
    """JSON-safe stamp for a policy_table.v1 ``config`` block."""

    if market not in MAX_TABLE_AGE_HOURS:
        raise KeyError(
            f"MAX_TABLE_AGE not defined for market={market!r}; "
            f"known={sorted(MAX_TABLE_AGE_HOURS)} ({CONTRACT_V11_SECTION_2_2})"
        )
    return {
        "max_table_age_hours": MAX_TABLE_AGE_HOURS[market],
        "max_table_age_source": CONTRACT_V11_SECTION_2_2,
        "max_table_age_note": (
            "Contract v1.1 §2-2: table missing / STALE marker / age > "
            f"{MAX_TABLE_AGE_HOURS[market]}h → that order cycle emits 0 orders "
            "(no quiet reuse/recompute). Consumer: scripts/b0x/table_source.py."
        ),
    }


__all__ = [
    "CONTRACT_V11_SECTION_2_2",
    "MAX_TABLE_AGE",
    "MAX_TABLE_AGE_HOURS",
    "max_table_age_stamp",
]
