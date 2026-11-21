"""Upbit KRW shadow strategy: pure rule library (hk 5852 design §9 T1).

Pure functions only: no DB, broker, network, scheduler or settings imports
(``tests/services/upbit_krw_strategy/test_static_boundary.py``). Every money,
price and ratio is a :class:`~decimal.Decimal`. Design:
``docs/design/5852-upbit-krw-shadow-strategy.md``.
"""

from .constants import RULES_VERSION

__all__ = ["RULES_VERSION"]
