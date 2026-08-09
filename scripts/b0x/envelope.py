"""B0-X §4 execution envelopes — hard invariants, not operator-tunable dials.

Contract §4 fixes a per-market safety envelope *above* B0's own sizing. This
module is the single place those numbers exist, and they are deliberately
**not reachable from any CLI flag or environment variable**:

  * ``CRYPTO_SIDECAR_ENVELOPE`` is a frozen module constant.
  * ``assert_envelope_locked`` fails closed BEFORE any network/DB call if a
    caller (a future adapter, a test, a scheduler) hands over anything else.
  * ``scripts/run_b0x_cycle.py`` exposes no flag whose value can reach an
    ``Envelope`` field, and ``load_envelope`` ignores ``os.environ`` entirely.

Pattern follows ROB-993's ``sizing.assert_leg_notional_cap_locked`` /
``kill_switch.assert_kill_switch_limits_locked``, which exist for the same
reason: a widened cap is the one bug class that converts a bounded experiment
into an unbounded one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

#: How ``daily_loss_kill`` is denominated.
#:
#: ``"absolute"`` (crypto's contract §4 value: *일 손실 5 USDT*) — the field is
#: a currency amount in ``quote_currency``, compared directly against
#: ``state.realized_pnl_today`` (same currency).
#:
#: ``"pct_of_nav"`` (KR's contract §4 value: *일 손실 −2.5% NAV*) — the field
#: is a dimensionless ratio in (0, 1). It cannot be compared against
#: ``realized_pnl_today`` directly (a ratio has no currency); ``kill_switch``
#: multiplies it by a same-cycle NAV snapshot to get an absolute threshold in
#: the *same* currency as ``realized_pnl_today`` before comparing. Mixing this
#: up — comparing a raw ratio, or an absolute threshold denominated in a
#: different currency than the P&L it is compared against — is the exact
#: defect X-C's verification found in the crypto shadow lane (a USDT constant
#: compared against a KRW-denominated P&L). See ``kill_switch.evaluate``.
DailyLossKillBasis = Literal["absolute", "pct_of_nav"]


@dataclass(frozen=True, slots=True)
class Envelope:
    """Per-market execution envelope (contract §4 column)."""

    market: str
    quote_currency: str
    #: 주문당 신규 notional 상한.
    per_order_notional: Decimal
    #: 종목당 누적 투입 상한 (신규 + 물타기 전부 합산).
    per_symbol_total_notional: Decimal
    #: 동시에 보유할 수 있는 B0-X 포지션 수.
    max_concurrent_positions: int
    #: UTC 일자당 신규 *진입 종목* 수 (같은 종목의 L1/L2 사다리는 1건으로 센다).
    max_new_entries_per_utc_day: int
    #: kill switch 발화 임계값. ``daily_loss_kill_basis`` 가 그 단위를 정한다.
    daily_loss_kill: Decimal
    #: 위 필드의 단위. 기본값 ``"absolute"`` 는 crypto 의 기존 동작을 그대로
    #: 보존한다 — 이 필드를 추가하기 전까지 존재하던 유일한 의미였다.
    daily_loss_kill_basis: DailyLossKillBasis = "absolute"

    def __post_init__(self) -> None:  # pragma: no cover - trivially exercised
        if self.per_order_notional <= 0:
            raise ValueError("per_order_notional must be > 0")
        if self.per_symbol_total_notional < self.per_order_notional:
            raise ValueError("per_symbol_total_notional must be >= per_order_notional")
        if self.max_concurrent_positions <= 0:
            raise ValueError("max_concurrent_positions must be > 0")
        if self.max_new_entries_per_utc_day <= 0:
            raise ValueError("max_new_entries_per_utc_day must be > 0")
        if self.daily_loss_kill <= 0:
            raise ValueError("daily_loss_kill must be > 0")
        if self.daily_loss_kill_basis == "pct_of_nav" and self.daily_loss_kill >= 1:
            raise ValueError(
                "daily_loss_kill_basis='pct_of_nav' requires a ratio in (0, 1); "
                f"got {self.daily_loss_kill!r} — this field is not a currency amount"
            )

    def canonical(self) -> dict[str, str | int]:
        """Stable dict used for hashing and artifact stamping."""

        return {
            "market": self.market,
            "quote_currency": self.quote_currency,
            "per_order_notional": format(self.per_order_notional, "f"),
            "per_symbol_total_notional": format(self.per_symbol_total_notional, "f"),
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_new_entries_per_utc_day": self.max_new_entries_per_utc_day,
            "daily_loss_kill": format(self.daily_loss_kill, "f"),
            "daily_loss_kill_basis": self.daily_loss_kill_basis,
        }


# ---------------------------------------------------------------------------
# The locked values. Contract §4, crypto 사이드카 column, verbatim:
#   주문 10 USDT · 종목 총 50 USDT · 동시 포지션 ≤ 3 · 일 신규 ≤ 2 · 일 손실 5 USDT → kill
# ---------------------------------------------------------------------------
CRYPTO_SIDECAR_ENVELOPE: Final[Envelope] = Envelope(
    market="crypto",
    quote_currency="USDT",
    per_order_notional=Decimal("10"),
    per_symbol_total_notional=Decimal("50"),
    max_concurrent_positions=3,
    max_new_entries_per_utc_day=2,
    daily_loss_kill=Decimal("5"),
)

# ---------------------------------------------------------------------------
# Contract §4, KR (kis_mock) column, verbatim:
#   종목당 신규 30만 KRW · 물타기 회차 상한 없음 단 종목당 총투입 ≤ 신규×5 ·
#   동시 포지션 ≤ 10 · 일 신규 진입 ≤ 3 · 일 손실 −2.5% NAV → kill
#
# "물타기 회차 상한 없음" is not a cap this dataclass can express (it means
# ``config.averaging_k_levels`` from the table is unbounded, not that a field
# here is infinite) — the per-symbol total notional cap is what actually
# bounds cumulative averaging spend, and it is present below.
# ---------------------------------------------------------------------------
KR_MOCK_ENVELOPE: Final[Envelope] = Envelope(
    market="kr",
    quote_currency="KRW",
    per_order_notional=Decimal("300000"),
    per_symbol_total_notional=Decimal("300000") * 5,
    max_concurrent_positions=10,
    max_new_entries_per_utc_day=3,
    # A ratio, not a KRW amount — see DailyLossKillBasis. kill_switch.evaluate
    # multiplies this by a same-cycle NAV snapshot to get the KRW threshold it
    # compares against KRW-denominated realized_pnl_today.
    daily_loss_kill=Decimal("0.025"),
    daily_loss_kill_basis="pct_of_nav",
)

# ---------------------------------------------------------------------------
# Contract §4, US (alpaca_paper_lab) column, verbatim:
#   종목당 신규 $150~450 · 종목당 총투입 = 신규×5 · 동시 포지션 ≤10 ·
#   일 신규 진입 ≤3 · 일 손실 −2.5% NAV → kill · 세션 US RTH
#
# ``per_order_notional`` is the immutable *upper* bound.  The lower bound and
# B0's selected $300 point inside the signed $150–450 band belong to the US
# venue planner, because ``Envelope`` deliberately models caps rather than a
# venue's lot/price-aware sizing rule.  Keeping that distinction explicit
# prevents a caller from mistaking $450 for the requested order size.
# ---------------------------------------------------------------------------
US_ALPACA_PAPER_LAB_ENVELOPE: Final[Envelope] = Envelope(
    market="us",
    quote_currency="USD",
    per_order_notional=Decimal("450"),
    per_symbol_total_notional=Decimal("450") * 5,
    max_concurrent_positions=10,
    max_new_entries_per_utc_day=3,
    daily_loss_kill=Decimal("0.025"),
    daily_loss_kill_basis="pct_of_nav",
)

#: Contract §4 footnote — "Upbit shadow 는 합성이므로 envelope 미적용(기록만)".
#: The shadow lane still *records* the sidecar envelope alongside every cycle so
#: a reader can see what would have bound a real venue, but derivation does not
#: apply it. This sentinel makes that choice explicit rather than implicit.
SHADOW_ENVELOPE_NOT_APPLIED: Final[str] = (
    "envelope_not_applied_synthetic_lane (contract §4 footnote) — recorded only"
)

_LOCKED_ENVELOPES: Final[dict[str, Envelope]] = {
    "crypto": CRYPTO_SIDECAR_ENVELOPE,
    "kr": KR_MOCK_ENVELOPE,
    "us": US_ALPACA_PAPER_LAB_ENVELOPE,
}


class EnvelopeNotLocked(ValueError):
    """Raised when a supplied envelope deviates from the locked contract value."""


def assert_envelope_locked(envelope: Envelope) -> None:
    """Fail closed unless ``envelope`` is byte-identical to the §4 constant.

    Called at the top of every cycle entry point, before any broker/DB/network
    call, so a widened cap can never reach a venue.
    """

    locked = _LOCKED_ENVELOPES.get(envelope.market)
    if locked is None:
        raise EnvelopeNotLocked(
            f"no locked B0-X envelope for market {envelope.market!r} — "
            f"known markets: {sorted(_LOCKED_ENVELOPES)}"
        )
    if envelope != locked:
        raise EnvelopeNotLocked(
            f"envelope {envelope!r} deviates from the locked contract §4 "
            f"invariant {locked!r} — B0-X has no operator-tunable safety caps"
        )


def load_envelope(market: str) -> Envelope:
    """Return the locked envelope for ``market``.

    Deliberately takes no config/env/CLI input beyond the market name: there is
    no code path by which an operator value becomes an envelope value.
    """

    try:
        return _LOCKED_ENVELOPES[market]
    except KeyError as exc:
        raise EnvelopeNotLocked(
            f"no locked B0-X envelope for market {market!r} — "
            f"known markets: {sorted(_LOCKED_ENVELOPES)}"
        ) from exc


__all__ = [
    "DailyLossKillBasis",
    "Envelope",
    "EnvelopeNotLocked",
    "CRYPTO_SIDECAR_ENVELOPE",
    "KR_MOCK_ENVELOPE",
    "US_ALPACA_PAPER_LAB_ENVELOPE",
    "SHADOW_ENVELOPE_NOT_APPLIED",
    "assert_envelope_locked",
    "load_envelope",
]
