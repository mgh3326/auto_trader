import asyncio
import logging
import re
from datetime import date, datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossWarningInfo

logger = logging.getLogger(__name__)

# LIQUIDATION_TRADING is the only warning type that blocks orders
_BLOCKING_WARNING_TYPES = {"LIQUIDATION_TRADING"}


class WarningsGuardResult(NamedTuple):
    ok: bool
    warnings: list[TossWarningInfo]
    error_message: str | None = None


def _is_active_warning(warning: TossWarningInfo, today: date) -> bool:
    start = date.fromisoformat(warning.start_date) if warning.start_date else date.min
    end = date.fromisoformat(warning.end_date) if warning.end_date else None
    return start <= today and (end is None or today <= end)


async def check_warnings_guard(
    client: TossReadClient,
    symbol: str,
    market: str | None = None,
    timeout: float = 3.0,
    today: date | None = None,
) -> WarningsGuardResult:
    """
    Checks if there are active warnings for the given symbol.
    Blocks the order if LIQUIDATION_TRADING is active.
    Fail-open: If the API request fails or times out, allows the order to proceed.
    Only checks KR stock symbols (6 numeric digits).
    """
    clean_sym = str(symbol).strip()

    # market == "equity_kr" 종목만 실조회 수행 (US는 스킵하여 latency 최적화)
    is_kr = False
    if market == "kr" or market == "equity_kr":
        is_kr = True
    elif market is None:
        if re.match(r"^\d{6}$", clean_sym):
            is_kr = True

    if not is_kr:
        return WarningsGuardResult(ok=True, warnings=[])

    try:
        # Request with timeout
        raw_warnings = await asyncio.wait_for(
            client.warnings(clean_sym), timeout=timeout
        )
        current_date = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
        active_warnings = [
            warning
            for warning in raw_warnings
            if _is_active_warning(warning, current_date)
        ]

        # Check blocking warning types
        blocking = [
            w for w in active_warnings if w.warning_type in _BLOCKING_WARNING_TYPES
        ]
        if blocking:
            blocked_types = ", ".join(w.warning_type for w in blocking)
            return WarningsGuardResult(
                ok=False,
                warnings=active_warnings,
                error_message=f"Order blocked due to warning types: {blocked_types}",
            )

        return WarningsGuardResult(ok=True, warnings=active_warnings)
    except TimeoutError:
        logger.warning(
            "Timeout checking Toss warnings for symbol=%s. Proceeding (fail-open).",
            clean_sym,
        )
        return WarningsGuardResult(
            ok=True, warnings=[], error_message="Warnings check timed out (fail-open)"
        )
    except Exception as exc:
        logger.error(
            "Error checking Toss warnings for symbol=%s: %s. Proceeding (fail-open).",
            clean_sym,
            exc,
            exc_info=True,
        )
        return WarningsGuardResult(
            ok=True,
            warnings=[],
            error_message=f"Warnings check failed: {exc} (fail-open)",
        )
