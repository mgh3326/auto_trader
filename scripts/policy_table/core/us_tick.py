"""US listed-equity tick-size table, expressed as a D3-engine ``TickTable``.

US equities do not have a KRX-style multi-band ladder. The live order path
documents overseas pricing as "decimal USD (no tick table)"
(``app/mcp_server/README.md`` comparison table). For policy_table alignment
(buy L1 floor / sell-target minus one tick) the D3 ``TickTable`` container
still needs an explicit ladder, so this module encodes the Reg NMS Rule 612
minimum price variation for NMS stocks:

* price < $1.00  → tick $0.0001
* price ≥ $1.00  → tick $0.01

Source of record for this job (P-2-US / U-1): SEC Rule 612 (minimum pricing
increments) as the standard US listed-equity quoting floor used by broker
limit-order paths. Sub-penny quoting exceptions for dark pools / retail are
out of scope — advisory table alignment only.
"""

from __future__ import annotations

from research.kr_corpus.d3_engine.tick import TickTable

TICK_SOURCE = (
    "SEC Rule 612 NMS minimum price variation "
    "($0.0001 under $1 / $0.01 at-or-above $1) — "
    "scripts/policy_table/core/us_tick.py (no runtime US tick helper in app/)"
)


def build_us_equity_tick_table() -> TickTable:
    """Build a D3 ``TickTable`` for standard US listed equity quoting."""

    return TickTable.from_mapping(
        {
            "bands": [
                {
                    "lower_inclusive": "0",
                    "upper_exclusive": "1",
                    "tick": "0.0001",
                },
                {
                    "lower_inclusive": "1",
                    "upper_exclusive": None,
                    "tick": "0.01",
                },
            ]
        }
    )


__all__ = ["build_us_equity_tick_table", "TICK_SOURCE"]
