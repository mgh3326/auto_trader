"""A stateful, offline stand-in for the NH namuh mock order API (#711 tests).

It mimics the vendor's documented request/response *shapes* only; no live NH
response was captured.  Knobs simulate the failure modes the Stage 2 contract
must survive: the open-only listing returning ``[]`` while orders rest (the
Kiwoom ``kt00009`` lesson), error-shaped listings, and a send that times out
after the broker created the order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

MOCK_ACCOUNT = "MOCK-ACCOUNT-03"
LIVE_ACCOUNT = "LIVE-ACCOUNT-01"


@dataclass
class _Order:
    order_no: int
    symbol: str
    side_name: str
    qty: int
    price: int
    open_qty: int
    filled: int = 0
    cancelled: int = 0
    modified: int = 0
    original: int = 0
    kind: str = ""


@dataclass
class FakeNHMockBroker:
    open_scope_always_empty: bool = False
    open_scope_error: bool = False
    all_scope_error: bool = False
    timeout_after_create: bool = False
    fill_on_place: bool = False
    next_no: int = 1000100
    orders: list[_Order] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def order_paths(self) -> list[str]:
        return [path for path in self.paths() if "/order/" in path]

    def _new(self, **kwargs: Any) -> _Order:
        self.next_no += 1
        order = _Order(order_no=self.next_no, **kwargs)
        self.orders.append(order)
        return order

    def _find(self, number: int) -> _Order | None:
        return next((o for o in self.orders if o.order_no == number), None)

    @staticmethod
    def _row(order: _Order) -> dict[str, Any]:
        return {
            "itg_orr_no": order.order_no,
            "org_itg_orr_no": order.original,
            "iem_cd": order.symbol,
            "iem_nm": "TEST",
            "sby_dit_cd_nm": order.side_name,
            "cor_can_dit_cd_nm": order.kind,
            "orr_qty": order.qty,
            "orr_pr": order.price,
            "tot_cns_qty": order.filled,
            "cns_avg_uit_pr": order.price if order.filled else 0,
            "ny_cns_qty": order.open_qty,
            "cor_qty": str(order.modified),
            "can_qty": order.cancelled,
            "orr_tm": "093000000",
            "orr_rjt_rsn_cd_nm": "",
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content)["Input_0"] if request.content else {}
        if path == "/n2/acctinfo":
            return httpx.Response(
                200,
                json={
                    "rsp_cd": "00000",
                    "Output_0": [
                        {"acct_no": MOCK_ACCOUNT, "acct_type": "03"},
                        {"acct_no": LIVE_ACCOUNT, "acct_type": "01"},
                    ],
                },
            )
        assert body.get("act_no") == MOCK_ACCOUNT, "fake broker saw a non-mock account"
        if path in {"/krstock/order/v1/cashBuy", "/krstock/order/v1/cashSell"}:
            assert body["nmn_pr_tp_cd"] == "01"
            order = self._new(
                symbol=body["iem_cd"],
                side_name="현금매수" if path.endswith("cashBuy") else "현금매도",
                qty=body["orr_qty"],
                price=body["orr_pr"],
                open_qty=0 if self.fill_on_place else body["orr_qty"],
                filled=body["orr_qty"] if self.fill_on_place else 0,
            )
            if self.timeout_after_create:
                raise httpx.ReadTimeout("created, then timed out")
            return httpx.Response(
                200,
                json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": order.order_no}},
            )
        if path == "/krstock/order/v1/modify":
            original = self._find(body["org_mkt_orr_no"])
            if original is None or original.open_qty <= 0:
                return httpx.Response(200, json={"rsp_cd": "40001", "rsp_msg": "no"})
            qty = body["cor_qty"]
            original.open_qty -= qty
            original.modified += qty
            new = self._new(
                symbol=original.symbol,
                side_name=original.side_name,
                qty=qty,
                price=body["cor_pr"],
                open_qty=qty,
                original=original.order_no,
                kind="정정",
            )
            return httpx.Response(
                200, json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": new.order_no}}
            )
        if path == "/krstock/order/v1/cancel":
            target = self._find(body["org_mkt_orr_no"])
            if target is None or target.open_qty <= 0:
                return httpx.Response(200, json={"rsp_cd": "40002", "rsp_msg": "no"})
            qty = body.get("cor_qty", target.open_qty)
            target.open_qty -= qty
            target.cancelled += qty
            cancel = self._new(
                symbol=target.symbol,
                side_name=target.side_name,
                qty=qty,
                price=0,
                open_qty=0,
                original=target.order_no,
                kind="취소",
            )
            return httpx.Response(
                200,
                json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": cancel.order_no}},
            )
        if path == "/krstock/inquiry/v1/dailyOrderExecution":
            scope = body["ost_cns_dit"]
            if (scope == "0" and self.all_scope_error) or (
                scope == "2" and self.open_scope_error
            ):
                return httpx.Response(
                    200,
                    json={"rsp_cd": "00007", "rsp_msg": "처리중 오류가 발생했습니다."},
                )
            rows = [self._row(o) for o in self.orders]
            if scope == "2":
                rows = (
                    []
                    if self.open_scope_always_empty
                    else [r for r in rows if r["ny_cns_qty"] > 0]
                )
            elif scope == "1":
                rows = [r for r in rows if r["tot_cns_qty"] > 0]
            if not rows:
                return httpx.Response(
                    200, json={"rsp_cd": "13578", "rsp_msg": "조회할 내역이 없습니다."}
                )
            return httpx.Response(
                200,
                json={
                    "rsp_cd": "00000",
                    "Output_0": [{"cus_fnm": "CUSTOMER_NAME_MUST_NOT_LEAK"}],
                    "Output_1": rows,
                },
            )
        if path == "/krstock/inquiry/v1/balance":
            return httpx.Response(
                200,
                json={
                    "rsp_cd": "00000",
                    "Output_0": {"dca": 10_000_000, "orr_pbl_amt": "9950000"},
                    "Output_1": [
                        {
                            "iem_cd": "005930",
                            "itg_bnc_qty": 3,
                            "phs_pr": 50000,
                            "now_pr": 51000,
                            "eal_amt": 153000,
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error_code": "not_found"})
