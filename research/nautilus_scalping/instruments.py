"""ROB-316 spike — shared instrument definitions.

A single source of truth for the XRPUSDT spot instrument so that ingest,
backtest, and tests all agree on precision/fees. Mirrors the precision of
Binance Spot XRPUSDT (price 4dp, size 1dp) following the pattern of
``TestInstrumentProvider.adausdt_binance``.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.currencies import USDT, XRP
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money, Price, Quantity

# Demo scalping uses a flat fee assumption shared with cost.py (ROB-313 D3).
# 10 bps per side is conservative for a non-VIP / non-BNB-discount taker.
_FEE = Decimal("0.0010")


def xrpusdt_binance() -> CurrencyPair:
    """Binance Spot XRP/USDT for backtesting."""
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("XRPUSDT"), Venue("BINANCE")),
        raw_symbol=Symbol("XRPUSDT"),
        base_currency=XRP,
        quote_currency=USDT,
        price_precision=4,
        size_precision=1,
        price_increment=Price(0.0001, precision=4),
        size_increment=Quantity(0.1, precision=1),
        lot_size=Quantity(0.1, precision=1),
        max_quantity=Quantity(900_000, precision=1),
        min_quantity=Quantity(0.1, precision=1),
        max_notional=None,
        min_notional=Money(1.0, USDT),
        max_price=Price(1000, precision=4),
        min_price=Price(0.0001, precision=4),
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=_FEE,
        taker_fee=_FEE,
        ts_event=0,
        ts_init=0,
    )
