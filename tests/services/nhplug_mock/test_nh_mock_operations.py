# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""#849 nh_mock_* operations over the real #711 ledger and an independent fake NH.

The fake NH records every token, read, and order request at the transport
boundary; assertions read those records and the ledger rows directly, never a
value computed by the code under test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.brokers.nhplug.client as client_module
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.nhplug_mock import operations
from app.services.nhplug_mock.account_identity import KeyMaterial
from tests.services.nhplug_mock.test_dispatch_state_machine import (  # noqa: F401
    nhplug_engine,
)

pytestmark = pytest.mark.integration

ROOT_SECRET = "ops-test-root-secret-0123456789abcdef"
KEY_ID = "ops-test-key-id"
ACCT = "/n2/acctinfo"
BALANCE = "/krstock/inquiry/v1/balance"
LISTING = "/krstock/inquiry/v1/dailyOrderExecution"
BUY = "/krstock/order/v1/cashBuy"
MODIFY = "/krstock/order/v1/modify"
CANCEL = "/krstock/order/v1/cancel"
SCOPES = {"0": "all", "1": "filled", "2": "open"}


def seoul_today() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def order_row(
    number: int,
    *,
    qty: int = 1,
    price: int = 50000,
    filled: int = 0,
    open_qty: int | None = None,
    cancelled: int = 0,
    modified: int = 0,
    original: int = 0,
    symbol: str = "005930",
    side: str = "buy",
) -> dict[str, Any]:
    open_value = qty - filled - cancelled - modified if open_qty is None else open_qty
    return {
        "itg_orr_no": number,
        "iem_cd": symbol,
        "sby_dit_cd_nm": "현금매수" if side == "buy" else "현금매도",
        "orr_qty": qty,
        "orr_pr": price,
        "tot_cns_qty": filled,
        "ny_cns_qty": open_value,
        "can_qty": cancelled,
        "cor_qty": str(modified),
        "org_itg_orr_no": original,
        "cns_avg_uit_pr": 0,
        "orr_tm": "093015123",
        "orr_rjt_rsn_cd_nm": "",
        "cor_can_dit_cd_nm": "",
        "rmt_mkt_cd": "KRX",
        "sor_mkt_sli_yn": "N",
    }


@dataclass
class FakeNH:
    """Independent NH observer: account list, balance, listings, and orders."""

    account_no: str
    acct_rows: list[dict[str, Any]] | None = None
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)
    listing_override: dict[str, Any] = field(default_factory=dict)
    balance: dict[str, Any] | None = None
    order_numbers: list[str] = field(default_factory=lambda: ["1000123"])
    token_calls: int = 0
    reads: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    orders: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def network_calls(self) -> int:
        return self.token_calls + len(self.reads) + len(self.orders)

    def reads_of(self, path: str) -> int:
        return sum(1 for seen, _ in self.reads if seen == path)

    def _listing(self, scope: str) -> dict[str, Any]:
        if scope in self.listing_override:
            return self.listing_override[scope]
        rows = list(self.rows.values())
        if scope == "open":
            rows = [r for r in rows if r["ny_cns_qty"] > 0]
        elif scope == "filled":
            rows = [r for r in rows if r["tot_cns_qty"] > 0]
        return {"rsp_cd": "00000", "Output_1": rows}

    def _read(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.reads.append((request.url.path, body))
        if request.url.path == ACCT:
            rows = (
                self.acct_rows
                if self.acct_rows is not None
                else [{"acct_no": self.account_no, "acct_type": "03"}]
            )
            return httpx.Response(200, json={"rsp_cd": "00000", "Output_0": rows})
        if request.url.path == BALANCE:
            return httpx.Response(
                200,
                json=self.balance
                or {
                    "rsp_cd": "00000",
                    "Output_0": {"orr_pbl_amt": "9950000"},
                    "Output_1": [
                        {
                            "iem_cd": "005930",
                            "iem_nm": "삼성전자",
                            "itg_bnc_qty": 3,
                            "phs_pr": 50000,
                            "now_pr": 51000,
                            "eal_amt": 153000,
                        }
                    ],
                },
            )
        if request.url.path == LISTING:
            scope = SCOPES[body["Input_0"]["ost_cns_dit"]]
            return httpx.Response(200, json=self._listing(scope))
        return httpx.Response(404, json={})

    def client(self) -> NHPlugMockClient:
        async def token() -> str:
            self.token_calls += 1
            return "unit-test-token"

        return NHPlugMockClient(
            app_key="unit-test-app-key",
            app_secret="unit-test-app-secret",
            token_provider=token,
            transport=httpx.MockTransport(self._read),
        )

    def order_wire(self) -> httpx.AsyncBaseTransport:
        observer = self

        class Wire(httpx.AsyncBaseTransport):
            def arm(self, deadline: float) -> None:
                assert deadline > 0

            async def hard_close(self, timeout: float) -> None:
                return None

            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                observer.orders.append((request.url.path, json.loads(request.content)))
                number = observer.order_numbers.pop(0)
                return httpx.Response(
                    200,
                    json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": number}},
                    request=request,
                )

        return Wire()


@pytest_asyncio.fixture(scope="module")
async def ops_engine(nhplug_engine: AsyncEngine) -> AsyncEngine:
    key = KeyMaterial.from_root_secret(1, KEY_ID, ROOT_SECRET.encode("utf-8"))
    async with nhplug_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO review.nhplug_mock_key_version(key_version,key_id,key_check) "
                "VALUES (1,:id,:check)"
            ),
            {"id": KEY_ID, "check": key.check()},
        )
    return nhplug_engine


@pytest.fixture(autouse=True)
def stage2_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")
    for name in ("KEY", "TIME", "DB", "HOST", "VENDOR"):
        monkeypatch.setenv(f"NHPLUG_STAGE2_{name}_CONFIRMED", "true")
    monkeypatch.setenv("NHPLUG_STAGE2_ROOT_SECRET_V1", ROOT_SECRET)
    monkeypatch.setenv("NHPLUG_APP_KEY", "unit-test-app-key")
    monkeypatch.setenv("NHPLUG_APP_SECRET", "unit-test-app-secret")


def install(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    suffix: str,
    module: Any = operations,
    **kwargs: Any,
) -> FakeNH:
    account_no = "MOCK-" + suffix
    monkeypatch.setenv("NHPLUG_MOCK_ACCOUNT_NO", account_no)
    fake = FakeNH(account_no=account_no, **kwargs)
    monkeypatch.setattr(module, "_engine", lambda: engine)
    monkeypatch.setattr(module, "_new_client", lambda credentials: fake.client())
    monkeypatch.setattr(client_module, "GatedTransport", fake.order_wire)
    return fake


async def ledger_rows(engine: AsyncEngine, account_no: str) -> list[dict[str, Any]]:
    """Independent read: rows whose account binding resolves from this number."""

    key = KeyMaterial.from_root_secret(1, KEY_ID, ROOT_SECRET.encode("utf-8"))
    async with engine.connect() as conn:
        ref = (
            await conn.execute(
                text(
                    "SELECT account_ref FROM review.nhplug_mock_account_binding "
                    "WHERE key_version=1 AND binding=:b"
                ),
                {"b": key.binding(account_no)},
            )
        ).scalar_one_or_none()
        if ref is None:
            return []
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM review.nhplug_mock_order_ledger "
                        "WHERE account_ref=:ref ORDER BY id"
                    ),
                    {"ref": ref},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def key(suffix: str, n: int = 1) -> str:
    return f"k{n}_{suffix}_0123456789"[:64].replace(".", "_")


async def place(fake: FakeNH, suffix: str, **overrides: Any) -> dict[str, Any]:
    arguments = {
        "symbol": "005930",
        "side": "buy",
        "quantity": 1,
        "price": 50000,
        "order_type": "limit",
        "idempotency_key": key(suffix),
        "dry_run": False,
        "confirm": True,
    }
    arguments.update(overrides)
    return await operations.place_order(**arguments)


async def reconcile(day: date | None = None) -> dict[str, Any]:
    return await operations.reconcile_orders(
        order_date=(day or seoul_today()).strftime("%Y%m%d"),
        dry_run=False,
        confirm=True,
    )


# ---------------------------------------------------------------------------
# The Stage 2 round trip (fake NH): place -> reconcile -> detail/history ->
# modify -> reconcile -> cancel -> reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_place_modify_cancel_reconcile(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(
        monkeypatch,
        ops_engine,
        "roundtrip",
        order_numbers=["1000123", "1000124", "1000125"],
    )

    placed = await place(fake, "roundtrip")
    assert placed["status"] == "uncertain"
    assert placed["success"] is False
    assert placed["retry_allowed"] is False
    assert placed["ack_evidence_order_id"] == "1000123"
    assert fake.orders == [
        (
            BUY,
            {
                "Input_0": {
                    "act_no": "MOCK-roundtrip",
                    "iem_cd": "005930",
                    "orr_qty": 1,
                    "orr_pr": 50000,
                    "nmn_pr_tp_cd": "01",
                    "orr_cnd_dit_cd": "00",
                    "ssl_nmn_pr_dit_cd": "00",
                    "rmt_mkt_cd": "KRX",
                    "sor_mkt_sli_yn": "N",
                }
            },
        )
    ]

    # Same key again: the durable row answers, nothing is sent.
    again = await place(fake, "roundtrip")
    assert again["replayed_existing_row"] is True
    assert len(fake.orders) == 1

    # Before reconciliation the number is evidence only: modify is refused.
    early = await operations.modify_order(
        order_id="1000123",
        new_price=49000,
        new_quantity=1,
        idempotency_key=key("roundtrip", 2),
        dry_run=False,
        confirm=True,
    )
    assert early["error"] == "order_not_bound_reconcile_first"
    assert len(fake.orders) == 1

    fake.rows[1000123] = order_row(1000123)
    first = await reconcile()
    assert first["status"] == "reconciled", first
    rows = await ledger_rows(ops_engine, "MOCK-roundtrip")
    assert [(r["state"], r["broker_order_id"]) for r in rows] == [("open", "1000123")]

    detail = await operations.get_order_detail(order_id="1000123")
    assert detail["broker_view"] == "listed"
    assert detail["owned_by_ledger"] is True
    history = await operations.get_order_history()
    assert history["orders_state"] == "complete"
    assert history["open_orders_state"] == "present"
    assert [o["order_no"] for o in history["open_orders"]] == ["1000123"]

    modified = await operations.modify_order(
        order_id="1000123",
        new_price=49000,
        new_quantity=1,
        idempotency_key=key("roundtrip", 2),
        dry_run=False,
        confirm=True,
    )
    assert modified["status"] == "uncertain", modified
    assert modified["ack_evidence_order_id"] == "1000124"
    assert fake.orders[-1] == (
        MODIFY,
        {
            "Input_0": {
                "act_no": "MOCK-roundtrip",
                "org_mkt_orr_no": "1000123",
                "all_pat_dit_cd": "1",
                "iem_cd": "005930",
                "cor_qty": 1,
                "cor_pr": 49000,
                "sop_cnd_pr": 0,
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            }
        },
    )

    fake.rows[1000123] = order_row(1000123, modified=1)
    fake.rows[1000124] = order_row(1000124, price=49000, original=1000123)
    second = await reconcile()
    assert second["status"] == "reconciled", second
    rows = await ledger_rows(ops_engine, "MOCK-roundtrip")
    assert [(r["operation_kind"], r["state"]) for r in rows] == [
        ("place", "modified"),
        ("modify", "confirmed"),
    ]
    assert rows[0]["successor_order_id"] == "1000124"

    cancelled = await operations.cancel_order(
        order_id="1000124",
        idempotency_key=key("roundtrip", 3),
        dry_run=False,
        confirm=True,
    )
    assert cancelled["status"] == "uncertain", cancelled
    assert fake.orders[-1] == (
        CANCEL,
        {
            "Input_0": {
                "act_no": "MOCK-roundtrip",
                "org_mkt_orr_no": "1000124",
                "all_pat_dit_cd": "1",
                "iem_cd": "005930",
            }
        },
    )

    fake.rows[1000124] = order_row(1000124, price=49000, original=1000123, cancelled=1)
    fake.rows[1000125] = order_row(1000125, price=49000, original=1000124, cancelled=1)
    third = await reconcile()
    assert third["status"] == "reconciled", third
    rows = await ledger_rows(ops_engine, "MOCK-roundtrip")
    assert [(r["operation_kind"], r["state"]) for r in rows] == [
        ("place", "modified"),
        ("modify", "confirmed"),
        ("cancel", "confirmed"),
    ]
    assert [path for path, _ in fake.orders] == [BUY, MODIFY, CANCEL]


# ---------------------------------------------------------------------------
# AC2: limit only, refused before any network call
# ---------------------------------------------------------------------------

NON_LIMIT_ORDER_TYPES = (
    "market",
    "MARKET",
    "Limit",
    "LIMIT",
    "limit ",
    " limit",
    "01",
    "03",
    "00",
    "best",
    "",
    None,
    1,
    True,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", NON_LIMIT_ORDER_TYPES)
async def test_non_limit_place_is_refused_with_zero_network_and_no_row(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, order_type: object
) -> None:
    fake = install(monkeypatch, ops_engine, "nonlimit")
    result = await place(fake, "nonlimit", order_type=order_type)
    assert result["error"] == "limit_order_only"
    assert result["sent"] is False
    assert fake.network_calls == 0
    assert await ledger_rows(ops_engine, "MOCK-nonlimit") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("price", (None, 0, -1, True, "50000", 50000.0))
async def test_missing_or_non_integer_limit_price_is_refused_with_zero_network(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, price: object
) -> None:
    fake = install(monkeypatch, ops_engine, "noprice")
    result = await place(fake, "noprice", price=price)
    assert result["error"] == "limit_price_required"
    assert fake.network_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ("market", None, "LIMIT"))
async def test_non_limit_modify_and_preview_are_refused_with_zero_network(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, order_type: object
) -> None:
    fake = install(monkeypatch, ops_engine, "nonlimitmod")
    modified = await operations.modify_order(
        order_id="1000123",
        new_price=49000,
        new_quantity=1,
        order_type=order_type,
        idempotency_key=key("nonlimitmod"),
        dry_run=False,
        confirm=True,
    )
    preview = await operations.preview_order(
        symbol="005930", side="buy", quantity=1, price=50000, order_type=order_type
    )
    assert modified["error"] == preview["error"] == "limit_order_only"
    assert fake.network_calls == 0


# ---------------------------------------------------------------------------
# AC3: mock-only fail-closed before any order request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", ("NHPLUG_MOCK_ACCOUNT_NO", "NHPLUG_APP_KEY", "NHPLUG_APP_SECRET")
)
async def test_missing_identity_or_credential_is_refused_with_zero_network(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    fake = install(monkeypatch, ops_engine, "missing")
    monkeypatch.delenv(missing)
    for result in (
        await place(fake, "missing"),
        await operations.cancel_order(
            order_id="1000123",
            idempotency_key=key("missing"),
            dry_run=False,
            confirm=True,
        ),
    ):
        assert result["error"] == "credentials_missing"
        assert result["missing_env_keys"] == [missing]
    assert fake.network_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate",
    (
        "NHPLUG_MOCK_ENABLED",
        "NHPLUG_STAGE2_KEY_CONFIRMED",
        "NHPLUG_STAGE2_TIME_CONFIRMED",
        "NHPLUG_STAGE2_DB_CONFIRMED",
        "NHPLUG_STAGE2_HOST_CONFIRMED",
        "NHPLUG_STAGE2_VENDOR_CONFIRMED",
    ),
)
async def test_every_gate_off_refuses_with_zero_network(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    fake = install(monkeypatch, ops_engine, "gate")
    monkeypatch.setenv(gate, "True")  # only the exact "true" arms a gate
    result = await place(fake, "gate")
    assert result["error"] in {"mock_gate_disabled", "stage2_not_ready"}
    assert fake.network_calls == 0


@pytest.mark.asyncio
async def test_missing_root_secret_refuses_with_zero_network(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "nosecret")
    monkeypatch.delenv("NHPLUG_STAGE2_ROOT_SECRET_V1")
    result = await place(fake, "nosecret")
    assert result["error"] == "key_version_unavailable"
    assert fake.network_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acct_rows", "code"),
    (
        ([{"acct_no": "MOCK-live", "acct_type": "01"}], "mock_account_rejected"),
        ([{"acct_no": "MOCK-live", "acct_type": "02"}], "mock_account_rejected"),
        ([{"acct_no": "MOCK-live", "acct_type": "04"}], "mock_account_rejected"),
        ([{"acct_no": "MOCK-live", "acct_type": ""}], "mock_account_unverified"),
        ([{"acct_no": "OTHER-03", "acct_type": "03"}], "mock_account_rejected"),
        (
            [
                {"acct_no": "MOCK-live", "acct_type": "03"},
                {"acct_no": "MOCK-live", "acct_type": "01"},
            ],
            "mock_account_rejected",
        ),
        ([], "mock_account_rejected"),
    ),
)
async def test_non_mock_account_is_refused_after_one_acctinfo_read_and_no_order(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    acct_rows: list[dict[str, Any]],
    code: str,
) -> None:
    fake = install(monkeypatch, ops_engine, "live", acct_rows=acct_rows)
    results = [
        await place(fake, "live"),
        await operations.modify_order(
            order_id="1000123",
            new_price=49000,
            new_quantity=1,
            idempotency_key=key("live", 2),
            dry_run=False,
            confirm=True,
        ),
        await operations.cancel_order(
            order_id="1000123",
            idempotency_key=key("live", 3),
            dry_run=False,
            confirm=True,
        ),
    ]
    assert [r["error"] for r in results] == [code] * 3
    # Exactly one acctinfo read per call; no listing, balance, or order request.
    assert [path for path, _ in fake.reads] == [ACCT] * 3
    assert fake.orders == []
    assert await ledger_rows(ops_engine, "MOCK-live") == []


# ---------------------------------------------------------------------------
# AC4: an empty or unparsable response is unknown, never "nothing open"
# ---------------------------------------------------------------------------

# Complete listings with zero rows: usable pages, but never proof of absence.
EMPTY_LISTINGS = {
    "empty_array": {"rsp_cd": "00000", "Output_1": []},
    "block_absent": {"rsp_cd": "00000"},
    "no_records_13578": {"rsp_cd": "13578"},
}
# Unusable or incomplete listings.
UNKNOWN_LISTINGS = {
    "gateway_error": {"error_code": "500", "error_description": "gateway"},
    "unknown_code": {"rsp_cd": "00007"},
    "rows_not_list": {"rsp_cd": "00000", "Output_1": {"itg_orr_no": 1}},
    "malformed_row": {"rsp_cd": "00000", "Output_1": [{"itg_orr_no": "x"}]},
    "continuation_without_key": {"rsp_cd": "00165", "Output_1": []},
}


@pytest.mark.asyncio
async def test_empty_complete_listings_report_unknown_open_orders(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "emptyopen")
    history = await operations.get_order_history()
    assert history["orders_state"] == "complete"
    assert history["open_orders_state"] == "unknown"
    assert history["open_orders"] == []
    assert (
        "empty_open_listing_is_not_evidence_of_no_open_orders"
        in (history["open_orders_reasons"])
    )
    assert fake.orders == []


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(UNKNOWN_LISTINGS))
async def test_unusable_listing_is_unknown_in_history_and_reconcile(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    suffix = "unk" + label.replace("_", "")[:10]
    fake = install(monkeypatch, ops_engine, suffix, order_numbers=["1000200"])
    placed = await place(fake, suffix)
    assert placed["status"] == "uncertain"
    fake.listing_override = {
        "all": UNKNOWN_LISTINGS[label],
        "open": UNKNOWN_LISTINGS[label],
        "filled": UNKNOWN_LISTINGS[label],
    }

    history = await operations.get_order_history()
    assert history["success"] is False
    assert history["orders_state"] == "unknown"
    assert history["open_orders_state"] == "unknown"

    result = await reconcile()
    assert result["status"] == "unknown"
    assert result["success"] is False
    rows = await ledger_rows(ops_engine, "MOCK-" + suffix)
    assert [(r["state"], r["ack_evidence_order_id"]) for r in rows] == [
        ("uncertain", "1000200")
    ]
    assert result["unresolved_row_ids"] == [rows[0]["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(EMPTY_LISTINGS))
async def test_complete_but_empty_listing_never_resolves_an_uncertain_row(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    suffix = "emp" + label.replace("_", "")[:10]
    fake = install(monkeypatch, ops_engine, suffix, order_numbers=["1000300"])
    await place(fake, suffix)
    fake.listing_override = dict.fromkeys(
        ("all", "open", "filled"), EMPTY_LISTINGS[label]
    )
    history = await operations.get_order_history()
    assert history["open_orders_state"] == "unknown"
    assert history["open_orders"] == []
    assert (
        "ledger_has_order_with_unknown_broker_number" in history["open_orders_reasons"]
    )
    result = await reconcile()
    rows = await ledger_rows(ops_engine, "MOCK-" + suffix)
    assert [r["state"] for r in rows] == ["uncertain"]
    assert result["success"] is False
    assert result["open_orders_state"] == "unknown"
    assert result["unresolved_row_ids"] == [rows[0]["id"]]
    # The reservation still blocks a new key on the same symbol and side.
    blocked = await place(fake, suffix, idempotency_key=key(suffix, 2))
    assert blocked["error"] == "in_flight_order_exists"
    assert len(fake.orders) == 1


# ---------------------------------------------------------------------------
# AC5: modify/cancel only on owned, bound, still-open orders
# ---------------------------------------------------------------------------


async def bound_open_order(
    fake: FakeNH, engine: AsyncEngine, suffix: str, number: int
) -> None:
    fake.order_numbers = [str(number)]
    await place(fake, suffix)
    fake.rows[number] = order_row(number)
    await reconcile()
    rows = await ledger_rows(engine, fake.account_no)
    assert rows[-1]["state"] == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ("open", "filled"))
@pytest.mark.parametrize(
    "listing",
    (UNKNOWN_LISTINGS["gateway_error"], UNKNOWN_LISTINGS["continuation_without_key"]),
    ids=("gateway_error", "continuation_without_key"),
)
async def test_reconcile_with_an_incomplete_scope_is_never_reported_reconciled(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    listing: dict[str, Any],
) -> None:
    suffix = "rs" + scope[:4] + str(len(json.dumps(listing)))
    fake = install(monkeypatch, ops_engine, suffix)
    await bound_open_order(fake, ops_engine, suffix, 1000460)
    fake.listing_override = {scope: listing}
    result = await reconcile()
    assert result["success"] is False
    assert result["status"] == "unknown"
    assert scope in result["incomplete_scopes"]
    rows = await ledger_rows(ops_engine, "MOCK-" + suffix)
    assert [r["state"] for r in rows] == ["open"]
    history = await operations.get_order_history()
    if scope == "open":
        assert history["success"] is False
        assert history["status"] == "unknown"
        assert history["incomplete_scopes"] == ["open"]
        assert history["open_orders_state"] == "present"  # positive all-scope row
    if scope == "open":
        # The bound row was not re-verified, and the answer names it.
        assert result["unverified_row_ids"] == [rows[0]["id"]]
        assert result["rows"][0]["result"]["reconcile"] == "skipped"
    assert fake.orders == [(BUY, fake.orders[0][1])]


@pytest.mark.asyncio
async def test_modify_and_cancel_of_unowned_number_send_nothing(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "unowned")
    await bound_open_order(fake, ops_engine, "unowned", 1000400)
    reads_before = len(fake.reads)
    fake.rows[1000999] = order_row(1000999)  # a broker order we never sent
    for result in (
        await operations.modify_order(
            order_id="1000999",
            new_price=49000,
            new_quantity=1,
            idempotency_key=key("unowned", 2),
            dry_run=False,
            confirm=True,
        ),
        await operations.cancel_order(
            order_id="1000999",
            idempotency_key=key("unowned", 3),
            dry_run=False,
            confirm=True,
        ),
    ):
        assert result["error"] == "order_not_owned"
        assert result["sent"] is False
    # Only the two account verifications: no listing read, no order.
    assert [path for path, _ in fake.reads[reads_before:]] == [ACCT, ACCT]
    assert len(fake.orders) == 1


@pytest.mark.asyncio
async def test_other_accounts_order_is_not_owned(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "ownerA")
    await bound_open_order(fake, ops_engine, "ownerA", 1000410)
    other = install(monkeypatch, ops_engine, "ownerB")
    other.rows[1000410] = order_row(1000410)
    # Bind account B in the registry first, so the refusal below must come from
    # the ledger's account_ref scoping and not from a missing binding.
    history = await operations.get_order_history()
    assert history["status"] == "ok"
    key_material = KeyMaterial.from_root_secret(1, KEY_ID, ROOT_SECRET.encode("utf-8"))
    async with ops_engine.connect() as conn:
        bound = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM review.nhplug_mock_account_binding "
                    "WHERE key_version=1 AND binding=:b"
                ),
                {"b": key_material.binding("MOCK-ownerB")},
            )
        ).scalar_one()
    assert bound == 1
    for result in (
        await operations.cancel_order(
            order_id="1000410",
            idempotency_key=key("ownerB"),
            dry_run=False,
            confirm=True,
        ),
        await operations.modify_order(
            order_id="1000410",
            new_price=49000,
            new_quantity=1,
            idempotency_key=key("ownerB", 2),
            dry_run=False,
            confirm=True,
        ),
    ):
        assert result.get("error") == "order_not_owned"
    assert other.orders == []
    assert await ledger_rows(ops_engine, "MOCK-ownerB") == []


@pytest.mark.asyncio
async def test_cancel_refused_when_broker_no_longer_lists_it_open(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "notopen")
    await bound_open_order(fake, ops_engine, "notopen", 1000420)
    fake.rows[1000420] = order_row(1000420, filled=1)
    result = await operations.cancel_order(
        order_id="1000420",
        idempotency_key=key("notopen", 2),
        dry_run=False,
        confirm=True,
    )
    assert result["error"] == "order_not_open_on_broker"
    assert len(fake.orders) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing", "code"),
    (
        (UNKNOWN_LISTINGS["gateway_error"], "listing_incomplete"),
        (UNKNOWN_LISTINGS["continuation_without_key"], "listing_incomplete"),
        (EMPTY_LISTINGS["no_records_13578"], "order_not_listed"),
        (EMPTY_LISTINGS["empty_array"], "order_not_listed"),
    ),
)
async def test_cancel_refused_when_listing_cannot_show_it_open(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    listing: dict[str, Any],
    code: str,
) -> None:
    suffix = "inc" + code[:6] + str(len(json.dumps(listing)))
    fake = install(monkeypatch, ops_engine, suffix)
    await bound_open_order(fake, ops_engine, suffix, 1000430)
    fake.listing_override = {"all": listing}
    result = await operations.cancel_order(
        order_id="1000430",
        idempotency_key=key(suffix, 2),
        dry_run=False,
        confirm=True,
    )
    assert result["error"] == code
    assert len(fake.orders) == 1


@pytest.mark.asyncio
async def test_terminal_owned_order_is_not_modifiable_and_quantity_is_capped(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "terminal")
    await bound_open_order(fake, ops_engine, "terminal", 1000440)
    too_many = await operations.modify_order(
        order_id="1000440",
        new_price=49000,
        new_quantity=2,
        idempotency_key=key("terminal", 2),
        dry_run=False,
        confirm=True,
    )
    assert too_many["error"] == "quantity_exceeds_open"
    fake.rows[1000440] = order_row(1000440, filled=1)
    await reconcile()
    rows = await ledger_rows(ops_engine, "MOCK-terminal")
    assert rows[-1]["state"] == "filled"
    result = await operations.cancel_order(
        order_id="1000440",
        idempotency_key=key("terminal", 3),
        dry_run=False,
        confirm=True,
    )
    assert result["error"] == "order_not_modifiable"
    assert result["ledger_state"] == "filled"
    assert len(fake.orders) == 1


@pytest.mark.asyncio
async def test_partial_cancel_uses_partial_scope_and_quantity(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "partial", order_numbers=["1000450"])
    await place(fake, "partial", quantity=3)
    fake.rows[1000450] = order_row(1000450, qty=3)
    await reconcile()
    fake.order_numbers = ["1000451"]
    result = await operations.cancel_order(
        order_id="1000450",
        cancel_quantity=1,
        idempotency_key=key("partial", 2),
        dry_run=False,
        confirm=True,
    )
    assert result["status"] == "uncertain"
    assert fake.orders[-1] == (
        CANCEL,
        {
            "Input_0": {
                "act_no": "MOCK-partial",
                "org_mkt_orr_no": "1000450",
                "all_pat_dit_cd": "2",
                "iem_cd": "005930",
                "cor_qty": 1,
            }
        },
    )


# ---------------------------------------------------------------------------
# Dispatch failure is answered from the durable row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_failure_after_send_is_never_reported_as_not_submitted(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "recordfail", order_numbers=["1000500"])

    async def broken_record(self: Any, claim: Any, outcome: Any) -> bool:
        raise RuntimeError("injected record failure")

    from app.services.nhplug_mock.ledger import NHPlugMockLedger

    monkeypatch.setattr(NHPlugMockLedger, "record_final", broken_record)
    result = await place(fake, "recordfail")
    assert len(fake.orders) == 1
    assert result["status"] == "uncertain"
    assert result["state"] == "sending"
    assert result["retry_allowed"] is False
    assert "sent" not in result or result["sent"] is not False
    again = await place(fake, "recordfail")
    assert again["replayed_existing_row"] is True
    assert len(fake.orders) == 1


@pytest.mark.asyncio
async def test_pre_send_refusal_after_claim_reports_not_submitted(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "presend")

    async def no_token() -> str:
        return ""

    original = fake.client

    def client_without_order_token() -> NHPlugMockClient:
        client = original()
        verified = client.verify_and_bind_mock_account

        async def verify_then_drop_token(account_no: str) -> None:
            await verified(account_no)
            client._token_provider = no_token

        client.verify_and_bind_mock_account = verify_then_drop_token  # type: ignore[method-assign]
        return client

    monkeypatch.setattr(
        operations, "_new_client", lambda credentials: client_without_order_token()
    )
    result = await place(fake, "presend")
    assert result["status"] == "not_submitted"
    assert result["state"] == "withdrawn"
    assert result["dispatch_error"] == "oauth_token_unavailable"
    assert fake.orders == []


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positions_and_orderable_cash_come_from_the_verified_balance(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "reads")
    positions = await operations.get_positions()
    cash = await operations.get_orderable_cash()
    assert positions["positions_state"] == "reported"
    assert positions["positions"][0]["symbol"] == "005930"
    assert positions["positions"][0]["quantity"] == 3
    assert cash["cash"] == 9950000
    assert [path for path, _ in fake.reads] == [ACCT, BALANCE, ACCT, BALANCE]
    assert fake.orders == []


@pytest.mark.asyncio
async def test_unparsable_balance_is_unknown_not_zero(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(
        monkeypatch,
        ops_engine,
        "badbalance",
        balance={"rsp_cd": "00000", "Output_0": {"orr_pbl_amt": "N/A"}},
    )
    positions = await operations.get_positions()
    cash = await operations.get_orderable_cash()
    assert positions["positions_state"] == "unknown"
    assert cash["cash"] is None
    assert cash["status"] == "unknown"


@pytest.mark.asyncio
async def test_reads_refuse_a_live_account_before_the_balance_read(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(
        monkeypatch,
        ops_engine,
        "readlive",
        acct_rows=[{"acct_no": "MOCK-readlive", "acct_type": "01"}],
    )
    assert (await operations.get_positions())["error"] == "mock_account_rejected"
    assert (await operations.get_orderable_cash())["error"] == "mock_account_rejected"
    assert (await operations.get_order_history())["error"] == "mock_account_rejected"
    assert [path for path, _ in fake.reads] == [ACCT, ACCT, ACCT]


@pytest.mark.asyncio
async def test_reconcile_dry_run_reads_but_writes_nothing(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "dryrec", order_numbers=["1000600"])
    await place(fake, "dryrec")
    fake.rows[1000600] = order_row(1000600)
    before = await ledger_rows(ops_engine, "MOCK-dryrec")
    plan = await operations.reconcile_orders(dry_run=True)
    assert plan["status"] == "dry_run"
    assert plan["ledger_writes"] == 0
    assert [r["planned_action"] for r in plan["rows"]] == ["verify_own_number"]
    assert await ledger_rows(ops_engine, "MOCK-dryrec") == before
    refused = await operations.reconcile_orders(dry_run=False, confirm=False)
    assert refused["error"] == "confirm_required"
    assert await ledger_rows(ops_engine, "MOCK-dryrec") == before


@pytest.mark.asyncio
async def test_unexpected_pre_dispatch_failure_is_sanitized_and_sends_nothing(
    ops_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, ops_engine, "internal")

    async def exploding_keys(engine: Any) -> Any:
        raise RuntimeError("postgresql://user:secret@db/host must not leak")

    monkeypatch.setattr(operations, "load_retained_keys_from_env", exploding_keys)
    result = await place(fake, "internal")
    assert result["status"] == "error"
    assert result["error"] == "internal_error"
    assert result["error_type"] == "RuntimeError"
    assert result["sent"] is False
    assert "secret" not in json.dumps(result)
    assert fake.network_calls == 0
