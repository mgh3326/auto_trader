"""Bounded mock-account inquiry routes used by reconciliation and MCP reads."""

from __future__ import annotations

from typing import Any, Final

from app.services.brokers.nhplug.client import NHPlugMockClient

DAILY_ORDER_EXECUTION_PATH: Final[str] = "/krstock/inquiry/v1/dailyOrderExecution"


class NHDomesticInquiryClient:
    """Account reads delegate to the Stage 1 client and remain read-gated."""

    def __init__(self, client: NHPlugMockClient) -> None:
        self._client = client

    async def get_positions(self, *, act_no: str) -> dict[str, Any]:
        return await self._client.fetch_balance(act_no=act_no)

    async def get_orderable_cash(self, *, act_no: str) -> dict[str, Any]:
        return await self._client.fetch_balance(act_no=act_no)

    async def daily_order_execution(
        self, *, trade_date: str, act_no: str
    ) -> dict[str, Any]:
        # The Stage 1 generic dispatcher is deliberately not widened.  This
        # narrow method is injected at the private boundary only after its path
        # was reviewed here; reconciliation never accepts caller-selected URLs.
        return await self._client._post_readonly(  # noqa: SLF001
            path=DAILY_ORDER_EXECUTION_PATH,
            input_0={
                "orr_dt": trade_date,
                "act_no": act_no,
                "orr_mkt_cd": "00",
                "ost_cns_dit": "0",
            },
            act_no=act_no,
            allowed_paths=frozenset({DAILY_ORDER_EXECUTION_PATH}),
        )
