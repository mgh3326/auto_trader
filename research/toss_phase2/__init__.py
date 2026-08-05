"""Toss Phase 2 combined-KRX/NXT staging collector.

The collector writes Parquet staging only.  The separately invoked loader
snapshots completed fragments into the dedicated combined-KRX/NXT research
table; neither surface is a backtest input.
"""
