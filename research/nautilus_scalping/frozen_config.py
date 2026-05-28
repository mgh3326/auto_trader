"""ROB-351 (eng-review ex-ante enforcement) — frozen campaign config (pure, stdlib).

The ex-ante gate thresholds and the achievable-execution envelope are committed
HERE in PR1, before any PR2 OOS read exists. Splitting the commit (PR1) from the
run (PR2) makes "ex-ante" structurally enforceable: the run records
``config_hash()`` and any later tweak changes the hash, so an ex-post adjustment
to admit a near-miss candidate is detectable, not a promise.

Envelope numbers are the real Binance USDⓈ-M Futures Demo schedule (maker 2.0 /
taker 4.0 bps); the fill model is the queue-loss/adverse-selection scenario in
``maker_fill.py`` (not a hand-picked constant — eng-review Issue 3 / fill defensibility).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class CampaignConfig:
    # significance / multiple-testing
    target_t: float = 2.0
    fdr_alpha: float = 0.05
    block_size: int = 10
    # economic-triviality floor (Codex: sign>0 is too low)
    economic_triviality_floor_bps: float = 0.5
    # achievable-execution envelope (Binance USDⓈ-M demo)
    achievable_maker_bps: float = 2.0
    taker_bps: float = 4.0
    # PIT seasoning (raw units; see pit_universe)
    min_seasoning: int = 0
    # analytic taker fee grid
    fee_grid_bps: tuple[float, ...] = (10.0, 7.5, 5.0, 2.0, 0.0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fee_grid_bps"] = list(self.fee_grid_bps)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CampaignConfig:
        allowed = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in allowed}
        if "fee_grid_bps" in kw:
            kw["fee_grid_bps"] = tuple(kw["fee_grid_bps"])
        return cls(**kw)

    def config_hash(self) -> str:
        """SHA-256 over the sorted-key JSON of the config (reproducible)."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode()
        ).hexdigest()


# The frozen default committed in PR1. PR2 reads THIS; it must not be edited to
# admit a result after an OOS read (the hash would change — that is the guard).
FROZEN_CONFIG = CampaignConfig()
