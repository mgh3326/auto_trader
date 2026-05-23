"""ROB-298 PR 2 — Binance USD-M Futures Demo execution domain.

Sibling of ``spot_demo``. Independent env namespace
(``BINANCE_FUTURES_DEMO_*``), independent host allowlist
(``demo-fapi.binance.com`` only), independent transport. Shares only the
unified ``binance_demo_order_ledger`` table via ``BinanceDemoLedgerService``
(writes ``product='usdm_futures'`` rows).
"""
