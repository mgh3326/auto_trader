"""Upbit KRW intraday shadow recorder (hk 6036): forward evidence, no orders.

Evaluates the frozen ``upbit-intraday-v1`` rules (``research.upbit_intraday``) on
closed 15-minute bars from Upbit public data and records signals, held-back
candidates and virtual round trips. Nothing here submits, previews or books an
order, and no virtual row is a fill.

Import concrete modules directly; this package ``__init__`` stays import free.
"""
