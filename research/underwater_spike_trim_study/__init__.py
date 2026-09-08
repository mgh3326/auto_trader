"""Offline historical backtest of the §128 underwater-spike-trim shadow.

The package answers one pre-registered question — is it better to (1) keep
holding, (2) trim 10%, or (3) trim 10% and rebid at support — when an
underwater position prints a >=+12% day with RSI(14) >= 75 and no named
resistance overhead.

Everything here reads frozen corpus Parquet files only.  There is no network
call, no database session, no broker adapter, and no credential lookup on any
code path, so the study can never move a real position.
"""
