"""hk 6067 (6033 K4) — Binance Spot Demo read-only stream collector.

Read-only market-data and own-fill streams for the Spot Demo venue. Writes
only Redis streams (``book:binance_spot_demo:<SYM>``,
``fills:binance_spot_demo``) and an optional JSONL recording. There is no
order, cancel or ledger capability here: ``tests/.../test_static_guard.py``
pins the import closure.

Nothing is re-exported on purpose — importing this package must stay free of
network, Redis and credential access.
"""
