"""Token-scoped HTTP transport for the retro/v1 read-only projections."""

import re
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.services import retro_v1 as readers
from app.services.account_value import retro_scoreboard
from app.services.retro_v1 import RetroReadError

PREFIX = "/trading/api/retro/v1"
#: ``session_observation`` integers; their ranges are the reader's to check.
INTEGER_PARAMS = ("movers", "max_lag_seconds")
OPERATIONS = {
    "retro_day": ("market", "date"),
    "retro_symbol": ("symbol", "from", "to"),
    "retro_symbol_events": ("symbol", "known_cutoff", "from", "to"),
    "retro_funnel": ("market", "from", "to"),
    "retro_scoreboard": ("from", "to"),
    "retro_positions": ("cutoff_date",),
    "retro_trades": ("from", "to"),
    "retro_decisions": ("from", "to"),
    "retro_timeline": ("market", "date"),
    "session_observation": ("date",),
    "mock_experiments": (),
    "mock_round_trips": (),
    "get_bars": ("symbol", "tf", "from", "to"),
    "data_gaps": (),
}
READERS = {
    "retro_day": readers.read_retro_day,
    "retro_symbol": readers.read_retro_symbol,
    "retro_symbol_events": readers.read_retro_symbol_events,
    "retro_funnel": readers.read_retro_funnel,
    "retro_scoreboard": retro_scoreboard.read_retro_scoreboard,
    "retro_positions": readers.read_retro_positions,
    "retro_trades": readers.read_retro_trades,
    "retro_decisions": readers.read_retro_decisions,
    "retro_timeline": readers.read_retro_timeline,
    "session_observation": readers.read_session_observation,
    "mock_experiments": readers.read_mock_experiments,
    "mock_round_trips": readers.read_mock_round_trips,
    "get_bars": readers.read_get_bars,
    "data_gaps": readers.read_data_gaps,
}
router = APIRouter(prefix=PREFIX, tags=["retro-v1"])


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _query(request: Request, operation: str) -> dict[str, Any]:
    params = request.query_params
    required = OPERATIONS[operation]
    allowed = {*required, "limit", "cursor"}
    if operation == "retro_trades":
        allowed.update(("market", "strategy"))
    if operation == "retro_decisions":
        allowed.update(("market", "symbol"))
    if operation == "retro_timeline":
        allowed.add("symbol")
    if operation == "session_observation":
        allowed.update(("market", "symbols", *INTEGER_PARAMS))
    if operation == "mock_experiments":
        allowed.add("strategy_id")
    if operation == "mock_round_trips":
        allowed.update(("strategy_id", "venue"))
    if operation in {"retro_symbol", "retro_symbol_events", "get_bars"}:
        allowed.add("market")
    if set(params) - allowed or any(k not in params for k in required):
        raise RetroReadError("validation_error")
    if any(len(params.getlist(k)) != 1 for k in params):
        raise RetroReadError("validation_error")
    result: dict[str, Any] = dict(params)
    for key in ("date", "cutoff_date", "from", "to"):
        if key in result:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result[key]):
                    raise ValueError
                date.fromisoformat(result[key])
            except ValueError:
                raise RetroReadError("validation_error") from None
    if (
        "from" in result
        and not 0
        <= (date.fromisoformat(result["to"]) - date.fromisoformat(result["from"])).days
        < 366
    ):
        raise RetroReadError("validation_error")
    if "limit" in result:
        value = result["limit"]
        if not re.fullmatch(r"[0-9]+", value):
            raise RetroReadError("validation_error")
        significant = value.lstrip("0")
        if len(significant) > 3 or not significant or int(significant) > 500:
            raise RetroReadError("validation_error")
        result["limit"] = int(significant)
    for key in INTEGER_PARAMS:
        if key in result:
            if not re.fullmatch(r"[0-9]{1,5}", result[key]):
                raise RetroReadError("validation_error")
            result[key] = int(result[key])
    if "cursor" in result and not re.fullmatch(
        r"[A-Za-z0-9_.:\-]{1,128}", result["cursor"]
    ):
        raise RetroReadError("validation_error")
    if operation == "retro_scoreboard" and "cursor" in result:
        if not re.fullmatch(r"o[0-9]{1,11}", result["cursor"]):
            raise RetroReadError("validation_error")
    if "tf" in result and result["tf"] not in {"1m", "5m", "60m", "1d"}:
        raise RetroReadError("validation_error")
    if "known_cutoff" in result and not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
        result["known_cutoff"],
    ):
        raise RetroReadError("validation_error")
    if (
        operation in {"retro_symbol", "retro_symbol_events", "get_bars"}
        and "market" in result
        and result["market"] not in {"kr", "us", "crypto"}
    ):
        raise RetroReadError("validation_error")
    return result


def _endpoint(operation: str):
    async def endpoint(
        request: Request, session: Annotated[AsyncSession, Depends(get_db)]
    ):
        try:
            query = _query(request, operation)
            reader = READERS[operation]
            return await reader(session, **query)
        except RetroReadError as exc:
            return error(
                exc.code,
                exc.safe_message,
                422 if exc.code == "validation_error" else 503,
            )
        except DBAPIError as exc:
            original = exc.orig
            code = getattr(original, "sqlstate", None) or getattr(
                original, "pgcode", None
            )
            if code == "57014":
                return error("timeout", "Request timed out.", 503)
            return error("upstream_unavailable", "Upstream unavailable.", 503)
        except Exception:
            return error("upstream_unavailable", "Upstream unavailable.", 503)

    return endpoint


for _operation in OPERATIONS:
    router.add_api_route(
        f"/{_operation}", _endpoint(_operation), methods=["GET"], name=_operation
    )
