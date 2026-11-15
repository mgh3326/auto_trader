"""TREND-TSMOM-v1 Binance USD-M Futures Demo trend package (hk 3796 CT-3).

State and locked-constant core only. There is no order submission, executor,
CLI, scheduler or env gate here yet; the send path arrives with CT-4/CT-5.
"""

from .constants import IDENTITY

__all__ = ["IDENTITY"]
