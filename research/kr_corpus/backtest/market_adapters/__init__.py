"""Contract-backed US and crypto market adapters for sealed corpus wiring.

The modules in this package contain no data fetch or backtest execution path.
They bind sealed-corpus contracts to the shared holdout, SHA, schema, and
PIT guards in the parent harness. Mapping layers preserve units and refuse
invented turnover fields.
"""

from __future__ import annotations
