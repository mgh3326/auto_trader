"""ROB-317 — Binance Demo WebSocket scalping daemon (read-only hot path).

This package holds the read-only hot-path units: market-data stream
decoding, in-memory state, and the event-driven trigger. It MUST NOT import
any signed execution client, the demo_scalping_exec package, or the demo
ledger writer — that boundary is AST-enforced by
``tests/services/brokers/binance/demo/test_no_testnet_imports.py``. Only the
exec-side ws_bridge (slice 4) may reach mutation layers. See ROB-317 design
§3.
"""
