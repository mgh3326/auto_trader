"""Low-cardinality execution observations for the existing log pipeline.

Each stage emits a counter sample and a latency observation as logfmt. Loki can
aggregate the event count and compute latency quantiles without a new exporter.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, Literal

logger = logging.getLogger(__name__)

Venue = Literal["kis", "upbit", "kiwoom", "alpaca", "binance"]
Market = Literal["kr", "us", "crypto", "multi"]
AccountKind = Literal["live", "mock", "shadow"]
Stage = Literal["receive", "detect", "gate", "broker_accept", "fill_reconcile"]
Outcome = Literal["success", "failure", "blocked", "uncertain"]

_VENUES: Final = frozenset({"kis", "upbit", "kiwoom", "alpaca", "binance"})
_MARKETS: Final = frozenset({"kr", "us", "crypto", "multi"})
_ACCOUNT_KINDS: Final = frozenset({"live", "mock", "shadow"})
_STAGES: Final = frozenset(
    {"receive", "detect", "gate", "broker_accept", "fill_reconcile"}
)
_OUTCOMES: Final = frozenset({"success", "failure", "blocked", "uncertain"})


def record_execution_stage(
    *,
    venue: Venue,
    market: Market,
    account_kind: AccountKind,
    stage: Stage,
    outcome: Outcome,
    duration_seconds: float,
) -> None:
    """Emit fixed-name counter and duration observations, failing open.

    Values are deliberately validated against closed vocabularies before they
    become log fields. Identifiers and free-form values are not accepted.
    """
    try:
        if (
            venue not in _VENUES
            or market not in _MARKETS
            or account_kind not in _ACCOUNT_KINDS
            or stage not in _STAGES
            or outcome not in _OUTCOMES
            or isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds < 0
        ):
            return

        labels = (
            f"venue={venue} market={market} account_kind={account_kind} "
            f"stage={stage} outcome={outcome}"
        )
        logger.info("execution_metric metric=execution_stage_total %s value=1", labels)
        logger.info(
            "execution_metric metric=execution_stage_duration_seconds %s value=%.9f",
            labels,
            duration_seconds,
        )
    except Exception:  # noqa: BLE001 - telemetry cannot affect order execution
        return


class _StageOutcome:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: Outcome = "success"

    def mark(self, value: Outcome) -> None:
        self.value = value


@contextmanager
def execution_stage(
    *,
    venue: Venue,
    market: Market,
    account_kind: AccountKind,
    stage: Stage,
) -> Iterator[_StageOutcome]:
    """Time a sync or async-adjacent operation and emit even when it raises."""
    started = time.perf_counter()
    result = _StageOutcome()
    try:
        yield result
    except BaseException:
        result.mark("failure")
        raise
    finally:
        record_execution_stage(
            venue=venue,
            market=market,
            account_kind=account_kind,
            stage=stage,
            outcome=result.value,
            duration_seconds=time.perf_counter() - started,
        )


def account_kind_for_mode(
    account_mode: str | None, *, shadow: bool = False
) -> AccountKind | None:
    """Map execution modes to the bounded observability vocabulary."""
    if shadow:
        return "shadow"
    mode = (account_mode or "").strip().lower()
    if mode.endswith("_live") or mode == "live":
        return "live"
    if (
        mode.endswith("_mock")
        or mode.endswith("_demo")
        or mode
        in {
            "mock",
            "demo",
            "paper",
            "alpaca_paper",
        }
    ):
        return "mock"
    return None


def record_reconnect_recovery(
    *,
    venue: Venue,
    market: Market,
    account_kind: AccountKind,
    outcome: Literal["success", "failure"],
    duration_seconds: float,
) -> None:
    """Emit reconnect recovery count and duration observations."""
    try:
        if (
            venue not in _VENUES
            or market not in _MARKETS
            or account_kind not in _ACCOUNT_KINDS
            or outcome not in {"success", "failure"}
            or isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds < 0
        ):
            return
        labels = (
            f"venue={venue} market={market} account_kind={account_kind} "
            f"stage=reconnect_recovery outcome={outcome}"
        )
        logger.info(
            "execution_metric metric=execution_reconnect_recovery_total %s value=1",
            labels,
        )
        logger.info(
            "execution_metric metric=execution_reconnect_recovery_seconds %s value=%.9f",
            labels,
            duration_seconds,
        )
    except Exception:  # noqa: BLE001 - telemetry cannot affect socket recovery
        return


def record_stale_quote_block(
    *, venue: Venue, market: Market, account_kind: AccountKind
) -> None:
    """Count a fail-closed stale-quote gate without exposing quote identity."""
    try:
        if (
            venue not in _VENUES
            or market not in _MARKETS
            or account_kind not in _ACCOUNT_KINDS
        ):
            return
        logger.info(
            "execution_metric metric=execution_stale_quote_blocks_total "
            "venue=%s market=%s account_kind=%s stage=gate outcome=blocked "
            "reason=stale_quote value=1",
            venue,
            market,
            account_kind,
        )
    except Exception:  # noqa: BLE001 - telemetry cannot affect a safety block
        return


__all__ = [
    "account_kind_for_mode",
    "execution_stage",
    "record_reconnect_recovery",
    "record_execution_stage",
    "record_stale_quote_block",
]
