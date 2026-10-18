"""Cost model (prereg §3): sealed fee schedule + scenario slippage.

The fee schedule is an operator-supplied JSON file sealed at T0 (content and
sha256 go into the manifest). Without one, commission and regulatory fees are
unknown: the simulation still runs with slippage only, but every net-return
metric is reported as ``PENDING_FEE_SCHEDULE`` and the net tests do not start.

Slippage is a cost line, never a worse fill price -- the limit price P_i is
not raised (prereg §3). FX is not modelled: the USD ledgers are used as is.

Schedule shape (closed keys; decimals as strings)::

    {
      "schedule_id": "...", "source": "<citation>", "as_of": "YYYY-MM-DD",
      "by_source": {
        "toss_live_order_ledger": {"commission_rate": "0.001", "min_commission_usd": "0"},
        "live_order_ledger":      {"commission_rate": "0.0025", "min_commission_usd": "0"}
      },
      "sec_fee_rate": "0.0000278",
      "finra_taf_per_share": "0.000166",
      "finra_taf_max_usd": "8.30",
      "fx": "none_usd_ledger"
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from research.rob1391_us_resubmit_shadow import constants as K

_TOP_KEYS = {
    "schedule_id",
    "source",
    "as_of",
    "by_source",
    "sec_fee_rate",
    "finra_taf_per_share",
    "finra_taf_max_usd",
    "fx",
}
_SOURCE_KEYS = {"commission_rate", "min_commission_usd"}
# Every ledger that can supply a parent must carry its own fees; a schedule
# for some other source would silently price real trades at zero commission.
REQUIRED_SOURCES = frozenset({"toss_live_order_ledger", "live_order_ledger"})
_MAX_RATE = Decimal("0.05")


class FeeScheduleError(ValueError):
    pass


def _rate(value: Any, name: str, *, cap: Decimal | None = _MAX_RATE) -> Decimal:
    if not isinstance(value, str):
        raise FeeScheduleError(f"{name} must be a decimal string")
    try:
        out = Decimal(value)
    except Exception:
        raise FeeScheduleError(f"{name} is not a decimal") from None
    if not out.is_finite() or out < 0 or (cap is not None and out > cap):
        raise FeeScheduleError(f"{name} out of range")
    return out


@dataclass(frozen=True, slots=True)
class SourceFees:
    commission_rate: Decimal
    min_commission_usd: Decimal


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    schedule_id: str
    by_source: dict[str, SourceFees]
    sec_fee_rate: Decimal
    finra_taf_per_share: Decimal
    finra_taf_max_usd: Decimal

    @classmethod
    def parse(cls, raw: Any) -> FeeSchedule:
        if not isinstance(raw, dict) or set(raw) != _TOP_KEYS:
            raise FeeScheduleError(
                f"fee schedule keys must be exactly {sorted(_TOP_KEYS)}"
            )
        if raw["fx"] != "none_usd_ledger":
            raise FeeScheduleError("fx must be 'none_usd_ledger' (USD ledgers only)")
        for key in ("schedule_id", "source", "as_of"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise FeeScheduleError(f"{key} must be a non-empty string")
        by_source_raw = raw["by_source"]
        if (
            not isinstance(by_source_raw, dict)
            or set(by_source_raw) != REQUIRED_SOURCES
        ):
            raise FeeScheduleError(
                f"by_source keys must be exactly {sorted(REQUIRED_SOURCES)}"
            )
        by_source: dict[str, SourceFees] = {}
        for source, fees in by_source_raw.items():
            if not isinstance(fees, dict) or set(fees) != _SOURCE_KEYS:
                raise FeeScheduleError(
                    f"by_source[{source}] keys must be {sorted(_SOURCE_KEYS)}"
                )
            by_source[source] = SourceFees(
                commission_rate=_rate(fees["commission_rate"], "commission_rate"),
                min_commission_usd=_rate(
                    fees["min_commission_usd"], "min_commission_usd", cap=Decimal("50")
                ),
            )
        return cls(
            schedule_id=raw["schedule_id"],
            by_source=by_source,
            sec_fee_rate=_rate(raw["sec_fee_rate"], "sec_fee_rate"),
            finra_taf_per_share=_rate(
                raw["finra_taf_per_share"], "finra_taf_per_share"
            ),
            finra_taf_max_usd=_rate(
                raw["finra_taf_max_usd"], "finra_taf_max_usd", cap=Decimal("100")
            ),
        )


@dataclass(frozen=True, slots=True)
class CostModel:
    """Costs for one source under one slippage scenario."""

    schedule: FeeSchedule | None
    source: str
    slippage_bp: Decimal

    def __post_init__(self) -> None:
        if self.schedule is not None and self.source not in self.schedule.by_source:
            raise FeeScheduleError(
                f"sealed fee schedule has no entry for {self.source}"
            )

    @property
    def fees_known(self) -> bool:
        return self.schedule is not None

    def _commission(self, notional: Decimal) -> Decimal:
        if not self.fees_known:
            return Decimal("0")
        assert self.schedule is not None
        fees = self.schedule.by_source[self.source]
        if notional <= 0:
            return Decimal("0")
        return max(notional * fees.commission_rate, fees.min_commission_usd)

    def _slippage(self, notional: Decimal) -> Decimal:
        return notional * self.slippage_bp * K.BP

    def buy_cost(self, notional: Decimal) -> Decimal:
        return self._commission(notional) + self._slippage(notional)

    def sell_cost(self, notional: Decimal, shares: int) -> Decimal:
        if shares <= 0:
            return Decimal("0")
        regulatory = Decimal("0")
        if self.fees_known:
            assert self.schedule is not None
            regulatory = notional * self.schedule.sec_fee_rate + min(
                self.schedule.finra_taf_per_share * shares,
                self.schedule.finra_taf_max_usd,
            )
        return self._commission(notional) + regulatory + self._slippage(notional)

    def affordable_shares(
        self, *, price: Decimal, remaining_budget: Decimal, include_cost: bool
    ) -> int:
        """Largest integer n with n*price (+ buy cost if ``include_cost``) <= budget."""

        if price <= 0 or remaining_budget <= 0:
            return 0
        n = int((remaining_budget / price).to_integral_value(rounding=ROUND_FLOOR))
        while n > 0:
            notional = price * n
            spend = notional + (
                self.buy_cost(notional) if include_cost else Decimal("0")
            )
            if spend <= remaining_budget:
                return n
            n -= 1
        return 0
