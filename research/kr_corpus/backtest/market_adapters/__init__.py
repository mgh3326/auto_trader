"""Contract-backed US and crypto market adapters for the fixture-only harness.

The modules in this package contain no data fetch or backtest execution path.
They bind declared, inferred contracts to the shared holdout, SHA, schema, and
PIT guards in the parent harness.
"""

from __future__ import annotations
