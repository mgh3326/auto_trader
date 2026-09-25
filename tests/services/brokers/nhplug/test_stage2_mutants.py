"""Mutation harness for the NHPLUG Stage 2 safety boundaries (#711, AC4).

Each mutant textually weakens exactly one boundary in a *copy* of the real
module source, loads the copy as a throwaway module, and runs the same
boundary probe that passes against the real module.  Every probe must turn
RED with an ``AssertionError`` on its mutant — never an import error and never
a silent pass — so the boundary tests are proven to detect the regression.

Required mutants: remove host re-verify, accept acct_type 01, drop
confirm=True, allow redirects, accept market order.  Two "empty array" mutants
cover the kt00009 rule as well.
"""

from __future__ import annotations

import importlib
import sys
import types
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.services.brokers.nhplug.contracts import DryRunConfirmContract
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockDispatchUncertain,
    NHPlugMockEndpointError,
    NHPlugMockOrderRefused,
)
from tests.mcp_server._registration_recorder import RegistrationRecorder

pytestmark = pytest.mark.unit

MOCK_ACCOUNT = "MOCK-ACCOUNT-03"
CONFIRMED = DryRunConfirmContract(dry_run=False, confirm=True)
ACCOUNTS = {
    "rsp_cd": "00000",
    "Output_0": [
        {"acct_no": MOCK_ACCOUNT, "acct_type": "03"},
        {"acct_no": "LIVE-ACCOUNT-01", "acct_type": "01"},
    ],
}

CLIENT = "app.services.brokers.nhplug.client"
GUARD = "app.services.brokers.nhplug.account_guard"
EVIDENCE = "app.services.brokers.nhplug.order_evidence"
OPERATIONS = "app.services.nhplug_mock.operations"
TOOLS = "app.mcp_server.tooling.orders_nhplug_mock_variants"


@contextmanager
def loaded(module_name: str, old: str | None = None, new: str = "") -> Iterator[Any]:
    """Yield the real module, or a fresh copy with one exact textual mutation."""

    real = importlib.import_module(module_name)
    if old is None:
        yield real
        return
    source = Path(str(real.__file__)).read_text(encoding="utf-8")
    assert source.count(old) == 1, (
        f"mutant anchor must match exactly once in {module_name}"
    )
    name = f"{module_name}_mutant_{uuid.uuid4().hex}"
    module = types.ModuleType(name)
    module.__file__ = real.__file__
    sys.modules[name] = module
    try:
        exec(
            compile(source.replace(old, new), str(real.__file__), "exec"),
            module.__dict__,
        )
        yield module
    finally:
        sys.modules.pop(name, None)


class _Broker:
    def __init__(
        self, respond: Callable[[httpx.Request], httpx.Response] | None = None
    ):
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            request.url.path == "/n2/acctinfo"
            and request.url.host == "moapi.nhplug.com"
        ):
            return httpx.Response(200, json=ACCOUNTS)
        if self._respond is not None:
            return self._respond(request)
        return httpx.Response(
            200, json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": 7}}
        )


async def _client(client_module: Any, broker: _Broker) -> tuple[Any, list[int]]:
    token_calls: list[int] = []

    async def tokens() -> str:
        token_calls.append(1)
        return "t"

    client = client_module.NHPlugMockClient(
        app_key="k",
        app_secret="s",
        token_provider=tokens,
        transport=httpx.MockTransport(broker),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NHPLUG_MOCK_ENABLED", "true")
        await client.verify_and_bind_mock_account(MOCK_ACCOUNT)
    token_calls.clear()
    broker.requests.clear()
    return client, token_calls


async def _submit(client: Any, authorization: Any = CONFIRMED) -> Any:
    return await client.submit_limit_order(
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        authorization=authorization,
    )


# --- probes: each returns normally iff the boundary holds -------------------


async def probe_host_reverify(
    client_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.AsyncClient.build_request

    def to_production(
        self: httpx.AsyncClient, method: str, _path: str, **kw: Any
    ) -> Any:
        return original(
            self, method, "https://api.nhplug.com:8443/krstock/order/v1/cashBuy", **kw
        )

    broker = _Broker()
    client, _ = await _client(client_module, broker)
    with monkeypatch.context() as patch:
        patch.setattr(httpx.AsyncClient, "build_request", to_production)
        try:
            await _submit(client)
        except NHPlugMockEndpointError:
            pass
        else:
            raise AssertionError("order sent without post-build host re-verification")
        assert broker.requests == [], "a request reached the transport"


async def probe_account_type(
    guard_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for account_type in ("01", "02"):
        try:
            guard_module.MockAccountAllowlist.from_acctinfo_response(
                payload={
                    "rsp_cd": "00000",
                    "Output_0": [{"acct_no": "A", "acct_type": account_type}],
                },
                configured_account_no="A",
            )
        except NHPlugMockAccountRejected:
            continue
        raise AssertionError(f"acct_type {account_type} was accepted")


async def probe_client_confirm(
    client_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for authorization in (
        DryRunConfirmContract(),
        DryRunConfirmContract(dry_run=False, confirm=False),
        DryRunConfirmContract(dry_run=True, confirm=True),
    ):
        broker = _Broker()
        client, token_calls = await _client(client_module, broker)
        try:
            await _submit(client, authorization)
        except NHPlugMockOrderRefused:
            pass
        else:
            raise AssertionError("client sent an order without confirm=True")
        assert token_calls == [] and broker.requests == []


async def probe_operations_confirm(
    ops_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[int] = []

    async def factory() -> Any:
        built.append(1)
        raise RuntimeError("must not build a client")

    result = await ops_module.place_limit_order(
        client_factory=factory,
        ledger=None,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=False,
    )
    assert result.get("error_code") == "confirm_required", result
    assert built == []


async def probe_tool_confirm(
    tools_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append("place")
        return {"success": True}

    @asynccontextmanager
    async def fake_ledger() -> Any:
        yield None

    real_ops = importlib.import_module(OPERATIONS)
    with monkeypatch.context() as patch:
        for key, value in (
            ("nhplug_mock_enabled", True),
            ("nhplug_app_key", "k"),
            ("nhplug_app_secret", "s"),
            ("nhplug_mock_account_no", "a"),
        ):
            patch.setattr(settings, key, value)
        patch.setitem(
            tools_module.__dict__,
            "operations",
            types.SimpleNamespace(
                validate_place_request=real_ops.validate_place_request,
                preview_place=real_ops.preview_place,
                place_limit_order=spy,
                SOURCE=real_ops.SOURCE,
                ACCOUNT_MODE=real_ops.ACCOUNT_MODE,
                NHPlugMockCredentials=real_ops.NHPlugMockCredentials,
            ),
        )
        patch.setitem(tools_module.__dict__, "_ledger", fake_ledger)
        recorder = RegistrationRecorder()
        tools_module.register(recorder)
        result = await recorder.tools["nhplug_mock_place_order"](
            symbol="005930",
            side="buy",
            quantity=1,
            price=50000,
            dry_run=False,
            confirm=False,
        )
    assert result.get("error_code") == "confirm_required", result
    assert calls == []


async def probe_redirect(client_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "moapi.nhplug.com":
            return httpx.Response(
                307,
                headers={
                    "Location": "https://api.nhplug.com:8443/krstock/order/v1/cashBuy"
                },
            )
        return httpx.Response(
            200, json={"rsp_cd": "00000", "Output_0": {"mkt_orr_no": 9}}
        )

    broker = _Broker(respond)
    client, _ = await _client(client_module, broker)
    try:
        await _submit(client)
    except NHPlugMockDispatchUncertain:
        pass
    else:
        raise AssertionError("an order redirect was followed")
    assert [r.url.host for r in broker.requests] == ["moapi.nhplug.com"]


async def probe_market_at_operations(
    ops_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for dry_run in (True, False):
        result = await ops_module.place_limit_order(
            client_factory=None,
            ledger=None,
            side="buy",
            symbol="005930",
            quantity=1,
            price=50000,
            order_type="market",
            dry_run=dry_run,
            confirm=True,
        )
        assert result.get("error_code") == "limit_orders_only", result


async def probe_market_at_client(
    client_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = httpx.AsyncClient.build_request

    def market(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> Any:
        body = kwargs["json"]
        kwargs["json"] = {"Input_0": {**body["Input_0"], "nmn_pr_tp_cd": "05"}}
        return original(self, *args, **kwargs)

    broker = _Broker()
    client, _ = await _client(client_module, broker)
    with monkeypatch.context() as patch:
        patch.setattr(httpx.AsyncClient, "build_request", market)
        try:
            await _submit(client)
        except NHPlugMockOrderRefused:
            pass
        else:
            raise AssertionError("a market-price-type body was sent")
        assert broker.requests == []


def _listing(evidence: Any, scope: str, payload: dict[str, Any]) -> Any:
    return evidence.assemble_listing(scope, [evidence.classify_listing_page(payload)])


_OPEN_ROW = {
    "itg_orr_no": 1000123,
    "iem_cd": "005930",
    "sby_dit_cd_nm": "현금매수",
    "orr_qty": 1,
    "orr_pr": 50000,
    "tot_cns_qty": 0,
    "ny_cns_qty": 1,
    "can_qty": 0,
}


async def probe_empty_open_scope(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kt00009: [] from the open scope must not hide a resting order."""

    result = evidence.determine_open_orders(
        all_listing=_listing(
            evidence, "all", {"rsp_cd": "00000", "Output_1": [_OPEN_ROW]}
        ),
        open_listing=_listing(evidence, "open", {"rsp_cd": "00000", "Output_1": []}),
    )
    assert result.state == "present", result


async def probe_error_shaped_is_unknown(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = {**_OPEN_ROW, "tot_cns_qty": 1, "ny_cns_qty": 0}
    result = evidence.determine_open_orders(
        all_listing=_listing(
            evidence, "all", {"rsp_cd": "00000", "Output_1": [closed]}
        ),
        open_listing=_listing(evidence, "open", {"rsp_cd": "00007", "Output_1": []}),
    )
    assert result.state == "unknown", result


# --- round-1 boundaries ----------------------------------------------------


async def probe_caller_bound_allowlist(
    client_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.brokers.nhplug.account_guard import MockAccountAllowlist

    broker = _Broker()
    client = client_module.NHPlugMockClient(
        app_key="k",
        app_secret="s",
        token_provider=_static_token,
        transport=httpx.MockTransport(broker),
    )
    client.bind_account_allowlist(
        MockAccountAllowlist.from_acctinfo_response(
            payload=ACCOUNTS, configured_account_no=MOCK_ACCOUNT
        )
    )
    try:
        await _submit(client)
    except NHPlugMockAccountRejected:
        pass
    else:
        raise AssertionError("a caller-bound allowlist enabled an order")
    assert broker.requests == []


async def _static_token() -> str:
    return "t"


async def probe_dispatcher_authorization(
    client_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _Broker()
    client, token_calls = await _client(client_module, broker)
    try:
        await client._post_mutation(
            path="/krstock/order/v1/cashBuy",
            input_0={
                "act_no": MOCK_ACCOUNT,
                "iem_cd": "005930",
                "orr_qty": 1,
                "orr_pr": 50000,
                "nmn_pr_tp_cd": "01",
                "orr_cnd_dit_cd": "00",
                "ssl_nmn_pr_dit_cd": "00",
                "rmt_mkt_cd": "KRX",
                "sor_mkt_sli_yn": "N",
            },
            authorization=DryRunConfirmContract(),
        )
    except NHPlugMockOrderRefused:
        pass
    else:
        raise AssertionError("the dispatcher sent without confirm=True")
    assert token_calls == [] and broker.requests == []


async def probe_wire_strict_types(
    tools_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client, FastMCP
    from fastmcp.exceptions import ToolError

    calls: list[str] = []

    async def spy(**kwargs: Any) -> dict[str, Any]:
        calls.append("place")
        return {"success": True}

    @asynccontextmanager
    async def fake_ledger() -> Any:
        yield None

    real_ops = importlib.import_module(OPERATIONS)
    with monkeypatch.context() as patch:
        for key, value in (
            ("nhplug_mock_enabled", True),
            ("nhplug_app_key", "k"),
            ("nhplug_app_secret", "s"),
            ("nhplug_mock_account_no", "a"),
        ):
            patch.setattr(settings, key, value)
        patch.setattr(real_ops, "place_limit_order", spy)
        patch.setitem(tools_module.__dict__, "_ledger", fake_ledger)
        server = FastMCP("mutant-wire")
        tools_module.register(server)
        async with Client(server) as client:
            try:
                await client.call_tool(
                    "nhplug_mock_place_order",
                    {
                        "symbol": "005930",
                        "side": "buy",
                        "quantity": 1,
                        "price": 50000,
                        "dry_run": 0,
                        "confirm": 1,
                    },
                )
            except ToolError:
                pass
            else:
                raise AssertionError("JSON 0/1 were coerced into dry_run/confirm")
    assert calls == []


async def probe_quantity_sum(evidence: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    row = evidence.parse_order_row({**_OPEN_ROW, "tot_cns_qty": 1, "ny_cns_qty": 1})
    assert evidence.derive_order_status(row) == "unknown"


async def probe_flag_without_key(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = evidence.classify_listing_page(
        {"rsp_cd": "00000"}, header_continuation_flag="Y"
    )
    assert evidence.assemble_listing("open", [page]).complete is False


async def probe_fill_cross_check(plan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    from app.services.brokers.nhplug import order_evidence as evidence

    filled = {**_OPEN_ROW, "tot_cns_qty": 1, "ny_cns_qty": 0, "cns_avg_uit_pr": 50000}
    planned = plan.plan_reconcile(
        [
            plan.LedgerRowView(
                id=1,
                operation_kind="place",
                status="accepted",
                symbol="005930",
                side="buy",
                quantity=1,
                price=Decimal(50000),
                broker_order_id="1000123",
                original_order_id=None,
            )
        ],
        all_listing=_listing(
            evidence, "all", {"rsp_cd": "00000", "Output_1": [filled]}
        ),
        open_listing=_listing(evidence, "open", {"rsp_cd": "13578"}),
        filled_listing=_listing(evidence, "filled", {"rsp_cd": "13578"}),
        all_claimed_order_ids=("1000123",),
    )
    assert planned[0].update.status is None, planned


Probe = Callable[[Any, pytest.MonkeyPatch], Awaitable[None]]

MUTANTS: tuple[tuple[str, str, str, str, Probe], ...] = (
    (
        "remove_host_reverify",
        CLIENT,
        "            _assert_resolved_mock_request(\n"
        "                request, allowed_paths=ALLOWED_MUTATION_PATHS, expected_path=path\n"
        "            )\n",
        "",
        probe_host_reverify,
    ),
    (
        "accept_acct_type_01",
        GUARD,
        'ALLOWED_MOCK_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset({"03"})\n'
        'DENIED_LIVE_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset({"01", "02"})\n',
        'ALLOWED_MOCK_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset({"03", "01", "02"})\n'
        "DENIED_LIVE_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset()\n",
        probe_account_type,
    ),
    (
        "drop_confirm_at_client",
        CLIENT,
        "        type(authorization) is not DryRunConfirmContract\n"
        "        or authorization.dry_run is not False\n"
        "        or authorization.confirm is not True\n",
        "        False\n",
        probe_client_confirm,
    ),
    (
        "drop_confirm_at_operations",
        OPERATIONS,
        "    if dry_run is False and confirm is not True:\n        return _error(\n"
        '            "confirm_required",',
        '    if False:\n        return _error(\n            "confirm_required",',
        probe_operations_confirm,
    ),
    (
        "drop_confirm_at_mcp_tool",
        TOOLS,
        '        if confirm is not True:\n            return _confirm_error("nhplug_mock_place_order")\n',
        "",
        probe_tool_confirm,
    ),
    (
        "allow_redirects_on_orders",
        CLIENT,
        "            # another origin; a 3xx is never followed.\n            follow_redirects=False,\n",
        "            # another origin; a 3xx is never followed.\n            follow_redirects=True,\n",
        probe_redirect,
    ),
    (
        "accept_market_order_type",
        OPERATIONS,
        "    if normalized != LIMIT_ORDER_TYPE:\n",
        '    if normalized not in {LIMIT_ORDER_TYPE, "market"}:\n',
        probe_market_at_operations,
    ),
    (
        "accept_market_price_code_in_body",
        CLIENT,
        '        if input_0.get("nmn_pr_tp_cd") != LIMIT_PRICE_TYPE_CODE:\n',
        '        if input_0.get("nmn_pr_tp_cd") not in {LIMIT_PRICE_TYPE_CODE, "05"}:\n',
        probe_market_at_client,
    ),
    (
        "trust_open_scope_only",
        EVIDENCE,
        "    if all_listing.complete:\n        for row in all_listing.rows:\n"
        "            if row.open_qty > 0:\n",
        "    if False:\n        for row in all_listing.rows:\n            if row.open_qty > 0:\n",
        probe_empty_open_scope,
    ),
    (
        "error_shaped_listing_counts_as_empty",
        EVIDENCE,
        "    if code not in READ_OK_CODES | READ_CONTINUE_CODES | {READ_NO_ROWS_CODE}:\n",
        "    if False:\n",
        probe_error_shaped_is_unknown,
    ),
    (
        "caller_bound_allowlist_enables_orders",
        CLIENT,
        "        allowlist = self._order_allowlist\n"
        "        if allowlist is None or allowlist is not self._account_allowlist:\n",
        "        allowlist = self._account_allowlist\n        if allowlist is None:\n",
        probe_caller_bound_allowlist,
    ),
    (
        "dispatcher_skips_authorization",
        CLIENT,
        "        _assert_send_authorized(authorization)\n        _assert_mock_enabled()\n",
        "        _assert_mock_enabled()\n",
        probe_dispatcher_authorization,
    ),
    (
        "wire_bool_coercion",
        TOOLS,
        "        dry_run: StrictBool = True,\n        confirm: StrictBool = False,\n        strategy",
        "        dry_run: bool = True,\n        confirm: bool = False,\n        strategy",
        probe_wire_strict_types,
    ),
    (
        "quantities_not_summed",
        EVIDENCE,
        "    if row.filled_qty + row.open_qty + cancelled + modified != row.order_qty:\n",
        "    if False:\n",
        probe_quantity_sum,
    ),
    (
        "continuation_flag_without_key_is_final",
        EVIDENCE,
        '    elif flag == "Y" or code in READ_CONTINUE_CODES:\n',
        '    elif (flag == "Y" or code in READ_CONTINUE_CODES) and key:\n',
        probe_flag_without_key,
    ),
    (
        "fill_without_filled_scope",
        "app.services.nhplug_mock.reconcile_plan",
        "    if (fill_gap := _fill_confirmed(broker_row, filled_listing)) is not None:\n",
        "    if (fill_gap := None) is not None:\n",
        probe_fill_cross_check,
    ),
)


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")


@pytest.mark.parametrize(
    ("label", "module", "old", "new", "probe"), MUTANTS, ids=[m[0] for m in MUTANTS]
)
@pytest.mark.asyncio
async def test_probe_passes_on_real_code(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    module: str,
    old: str,
    new: str,
    probe: Probe,
) -> None:
    with loaded(module) as real:
        await probe(real, monkeypatch)


@pytest.mark.parametrize(
    ("label", "module", "old", "new", "probe"), MUTANTS, ids=[m[0] for m in MUTANTS]
)
@pytest.mark.asyncio
async def test_mutant_turns_the_probe_red(
    armed: None,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    module: str,
    old: str,
    new: str,
    probe: Probe,
) -> None:
    with loaded(module, old, new) as mutant:
        with pytest.raises(AssertionError):
            await probe(mutant, monkeypatch)


def test_mutant_set_covers_every_required_boundary() -> None:
    labels = {m[0] for m in MUTANTS}
    assert {
        "remove_host_reverify",
        "accept_acct_type_01",
        "drop_confirm_at_client",
        "allow_redirects_on_orders",
        "accept_market_order_type",
    } <= labels
