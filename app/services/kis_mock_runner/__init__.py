"""Strategy-neutral, default-disabled KIS mock execution shell.

KR-B0 deliberately provides the safety boundary only.  It does not select a
strategy, symbol, price, or any other survivor-specific value; KR-B1 supplies
an immutable overlay only after the upstream candidate has passed.
"""

from .control import KillMode
from .overlay import OverlayBinding, OverlayRequired
from .runner import KISMockRunner, RunnerResult

__all__ = [
    "KISMockRunner",
    "KillMode",
    "OverlayBinding",
    "OverlayRequired",
    "RunnerResult",
]
