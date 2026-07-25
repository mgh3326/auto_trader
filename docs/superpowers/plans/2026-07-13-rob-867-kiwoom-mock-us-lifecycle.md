# ROB-867 Kiwoom Mock US Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Add a default-disabled, mock-host-only Kiwoom US account and order lifecycle under account_mode="kiwoom_mock_us", exposing only limit (00) and market (03) order types through MCP.

**Architecture:** Keep the existing Kiwoom mock HTTP transport and host guard, but construct a US-only client/auth instance from US-only credentials. Put US order payloads, account reads, MCP tools, and the operator smoke workflow in separate modules; share only broker-response shaping with the KR MCP module.

**Tech Stack:** Python 3.13+, FastMCP, httpx, Pydantic Settings, SQLAlchemy async symbol-universe lookup, pytest/pytest-asyncio, Ruff, ty, uv.

## Global Constraints

- Mock host only: every request must resolve to https://mockapi.kiwoom.com.
- KIWOOM_MOCK_US_ENABLED defaults to false and all US tools fail closed on incomplete US configuration.
- KIWOOM_MOCK_US_APP_KEY, KIWOOM_MOCK_US_APP_SECRET, and KIWOOM_MOCK_US_ACCOUNT_NO never fall back to KR mock credentials.
- MCP supports only trde_tp 00 and 03; all other codes fail before client construction or network I/O.
- dry_run=False requires confirm=True for place, modify, and cancel.
- Only NASDAQ/NASD, NYSE, and AMEX active universe rows are accepted.
- ust31490 is never called; USD deposit evidence is labeled deposit_not_broker_orderable.
- No secret, token, or account number may be logged or emitted.
- No database migration and no scheduled/automatic order-type probing.
- Preserve every existing KR kiwoom_mock public tool name, default, and response field.

---

## File Map

| Path | Responsibility | Change |
|---|---|---|
| app/core/config.py | US settings and missing-key validator | Modify |
| app/services/brokers/kiwoom/us_client.py | US-only settings factory over guarded transport | Create |
| app/services/brokers/kiwoom/constants.py | US TR ids, paths, and exchange codes | Modify |
| app/services/brokers/kiwoom/us_orders.py | US order payload building and mutation calls | Create |
| app/services/brokers/kiwoom/us_account.py | US account reads and USD deposit parsing | Create |
| app/mcp_server/tooling/orders_kiwoom_shared.py | KR/US broker response shaping | Create |
| app/mcp_server/tooling/orders_kiwoom_variants.py | Import shared response helpers; no contract change | Modify |
| app/mcp_server/tooling/orders_kiwoom_us_variants.py | Seven kiwoom_mock_us MCP tools | Create |
| app/mcp_server/tooling/registry.py | DEFAULT/KIWOOM profile registration | Modify |
| app/mcp_server/tooling/route_request_lanes.py | Read/mutation bucket classification | Modify |
| app/mcp_server/tooling/account_read_registration.py | Forbid US namespace in restricted profile | Modify |
| app/mcp_server/tooling/analysis_readonly_registration.py | Forbid US namespace in restricted profile | Modify |
| .claude/settings.readonly.json | Deny new preview/mutation tools | Modify |
| scripts/kiwoom_mock_us_smoke.py | Preflight, preview, full, and explicit probe workflow | Create |
| app/mcp_server/README.md | Public tool contract | Modify |
| docs/runbooks/kiwoom-mock-us-smoke.md | Operator procedure and evidence table | Create |
| tests/test_kiwoom_mock_us_config.py | Settings/credential isolation | Create |
| tests/test_kiwoom_us_orders.py | Order payload and validation tests | Create |
| tests/test_kiwoom_us_account.py | Account TR and cash parser tests | Create |
| tests/test_mcp_kiwoom_shared.py | Shared response behavior | Create |
| tests/test_mcp_kiwoom_us_order_variants.py | MCP contract and safety guards | Create |
| tests/test_kiwoom_mock_us_smoke_cli.py | CLI mutation gates and cleanup | Create |
| tests/test_mcp_profiles.py | Profile registration matrix | Modify |
| tests/test_route_request_registry_diff.py | Flag-gated read phantom allowance | Modify |
| tests/test_watch_triage_readonly_settings.py | Deny-list coverage | Modify |

---

### Task 1: Add US-only settings and guarded client factory

**Files:**
- Modify: app/core/config.py
- Create: app/services/brokers/kiwoom/us_client.py
- Create: tests/test_kiwoom_mock_us_config.py

**Interfaces:**
- Consumes: KiwoomMockClient(base_url, app_key, app_secret, account_no).
- Produces: validate_kiwoom_mock_us_config(settings_obj: Any = settings) -> list[str]; KiwoomMockUsClient.from_app_settings() -> KiwoomMockUsClient.

- [ ] **Step 1: Write failing configuration and credential-isolation tests**

~~~python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings, validate_kiwoom_mock_us_config
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomEndpointError,
)
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient


def test_settings_have_kiwoom_mock_us_defaults() -> None:
    assert settings.kiwoom_mock_us_enabled is False
    assert settings.kiwoom_mock_us_app_key is None
    assert settings.kiwoom_mock_us_app_secret is None
    assert settings.kiwoom_mock_us_account_no is None


def test_validator_reports_only_us_env_names() -> None:
    obj = SimpleNamespace(
        kiwoom_mock_us_enabled=False,
        kiwoom_mock_us_app_key=None,
        kiwoom_mock_us_app_secret="",
        kiwoom_mock_us_account_no=" ",
        kiwoom_mock_app_key="KR-AK",
        kiwoom_mock_app_secret="KR-SK",
        kiwoom_mock_account_no="KR-ACCOUNT",
    )
    assert validate_kiwoom_mock_us_config(obj) == [
        "KIWOOM_MOCK_US_ENABLED",
        "KIWOOM_MOCK_US_APP_KEY",
        "KIWOOM_MOCK_US_APP_SECRET",
        "KIWOOM_MOCK_US_ACCOUNT_NO",
    ]


def test_us_factory_never_falls_back_to_kr_credentials(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", "KR-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_secret", "KR-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_account_no", "KR-ACCOUNT")

    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockUsClient.from_app_settings()

    message = str(exc.value)
    assert "KIWOOM_MOCK_US_APP_KEY" in message
    assert "KR-AK" not in message
    assert "KR-SK" not in message
    assert "KR-ACCOUNT" not in message


def test_us_factory_builds_distinct_auth_instance(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT")

    first = KiwoomMockUsClient.from_app_settings()
    second = KiwoomMockUsClient.from_app_settings()

    assert first is not second
    assert first._auth is not second._auth


def test_us_factory_rejects_live_host(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT")
    monkeypatch.setattr(
        cfg.settings, "kiwoom_mock_base_url", "https://api.kiwoom.com"
    )
    with pytest.raises(KiwoomEndpointError):
        KiwoomMockUsClient.from_app_settings()
~~~

- [ ] **Step 2: Run the tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_mock_us_config.py -q
~~~

Expected: collection fails because validate_kiwoom_mock_us_config and us_client.py do not exist.

- [ ] **Step 3: Add settings and validator**

Insert beside the existing Kiwoom mock fields in app/core/config.py:

~~~python
    kiwoom_mock_us_enabled: bool = False
    kiwoom_mock_us_app_key: str | None = None
    kiwoom_mock_us_app_secret: str | None = None
    kiwoom_mock_us_account_no: str | None = None
~~~

Insert immediately after validate_kiwoom_mock_config:

~~~python
def validate_kiwoom_mock_us_config(settings_obj: Any = settings) -> list[str]:
    """Return missing Kiwoom US mock env names without exposing values."""

    missing: list[str] = []
    if not bool(getattr(settings_obj, "kiwoom_mock_us_enabled", False)):
        missing.append("KIWOOM_MOCK_US_ENABLED")
    if not _has_nonempty_value(
        getattr(settings_obj, "kiwoom_mock_us_app_key", None)
    ):
        missing.append("KIWOOM_MOCK_US_APP_KEY")
    if not _has_nonempty_value(
        getattr(settings_obj, "kiwoom_mock_us_app_secret", None)
    ):
        missing.append("KIWOOM_MOCK_US_APP_SECRET")
    if not _has_nonempty_value(
        getattr(settings_obj, "kiwoom_mock_us_account_no", None)
    ):
        missing.append("KIWOOM_MOCK_US_ACCOUNT_NO")
    return missing
~~~

- [ ] **Step 4: Create the US-only client factory**

~~~python
"""US-credential factory for Kiwoom mock transport."""

from __future__ import annotations

from typing import Self

from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomMockClient,
)


class KiwoomMockUsClient(KiwoomMockClient):
    """Kiwoom mock client constructed exclusively from US credentials."""

    @classmethod
    def from_app_settings(cls) -> Self:
        from app.core.config import settings, validate_kiwoom_mock_us_config

        missing = validate_kiwoom_mock_us_config(settings)
        if missing:
            raise KiwoomConfigurationError(
                "Kiwoom US mock account is disabled or missing required "
                "configuration: " + ", ".join(missing)
            )
        return cls(
            base_url=str(settings.kiwoom_mock_base_url).rstrip("/"),
            app_key=str(settings.kiwoom_mock_us_app_key),
            app_secret=str(settings.kiwoom_mock_us_app_secret),
            account_no=str(settings.kiwoom_mock_us_account_no),
        )
~~~

- [ ] **Step 5: Run tests and verify GREEN**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_mock_us_config.py tests/test_kiwoom_client_endpoint_guard.py tests/test_kiwoom_auth_token_cache.py -q
~~~

Expected: all tests pass; existing mock-host and secret-redaction tests remain green.

- [ ] **Step 6: Commit**

~~~bash
git add app/core/config.py app/services/brokers/kiwoom/us_client.py tests/test_kiwoom_mock_us_config.py
git commit -m "feat(ROB-867): isolate Kiwoom mock US credentials"
~~~

---

### Task 2: Implement documented US order payloads

**Files:**
- Modify: app/services/brokers/kiwoom/constants.py
- Create: app/services/brokers/kiwoom/us_orders.py
- Create: tests/test_kiwoom_us_orders.py

**Interfaces:**
- Consumes: any client implementing post_api(api_id, path, body, cont_yn, next_key).
- Produces: build_us_place_order_body(side: str, symbol: str, stex_tp: str, quantity: int, trde_tp: str, price: object | None, stop_price: object | None = None) -> dict[str, str]; validate_us_order_id(value: str) -> str; KiwoomUsOrderClient place/modify/cancel methods.

- [ ] **Step 1: Write failing payload and rejection tests**

~~~python
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.us_orders import (
    KiwoomUsOrderClient,
    KiwoomUsOrderRejected,
    build_us_place_order_body,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.account_no = "US-MOCK"

    async def post_api(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"return_code": 0, "ord_no": "000000282"}


def test_limit_and_market_body_format() -> None:
    assert build_us_place_order_body(
        side="buy",
        symbol="NVDA",
        stex_tp="ND",
        quantity=10,
        trde_tp="00",
        price=Decimal("213.0400"),
    ) == {
        "stex_tp": "ND",
        "stk_cd": "NVDA",
        "ord_qty": "10",
        "ord_uv": "213.0400",
        "trde_tp": "00",
    }
    assert build_us_place_order_body(
        side="buy",
        symbol="NVDA",
        stex_tp="ND",
        quantity=1,
        trde_tp="03",
        price=None,
    )["ord_uv"] == ""


def test_market_rejects_price_and_limit_requires_price() -> None:
    with pytest.raises(KiwoomUsOrderRejected, match="requires price"):
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp="ND",
            quantity=1,
            trde_tp="00",
            price=None,
        )
    with pytest.raises(KiwoomUsOrderRejected, match="must omit price"):
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp="ND",
            quantity=1,
            trde_tp="03",
            price=Decimal("1"),
        )


@pytest.mark.asyncio
async def test_buy_sell_modify_cancel_use_us_tr_ids() -> None:
    fake = FakeClient()
    client = KiwoomUsOrderClient(fake)

    await client.place_buy_order(
        symbol="NVDA", stex_tp="ND", quantity=1, trde_tp="00", price="213.04"
    )
    await client.place_sell_order(
        symbol="TSM", stex_tp="NY", quantity=2, trde_tp="03", price=None
    )
    await client.modify_order(
        original_order_no="000000282",
        symbol="NVDA",
        stex_tp="ND",
        new_price="210.00",
    )
    await client.cancel_order(
        original_order_no="000000283", symbol="NVDA", stex_tp="ND"
    )

    assert [call["api_id"] for call in fake.calls] == [
        constants.US_ORDER_BUY_API_ID,
        constants.US_ORDER_SELL_API_ID,
        constants.US_ORDER_MODIFY_API_ID,
        constants.US_ORDER_CANCEL_API_ID,
    ]
    assert all(call["path"] == constants.US_ORDER_PATH for call in fake.calls)
    assert fake.calls[2]["body"] == {
        "orig_ord_no": "000000282",
        "stex_tp": "ND",
        "stk_cd": "NVDA",
        "mdfy_uv": "210.00",
    }
    assert fake.calls[3]["body"] == {
        "orig_ord_no": "000000283",
        "stex_tp": "ND",
        "stk_cd": "NVDA",
    }


@pytest.mark.parametrize("exchange", ["KRX", "NXT", "NASD", ""])
def test_rejects_non_kiwoom_us_exchange(exchange: str) -> None:
    with pytest.raises(KiwoomUsOrderRejected, match="stex_tp"):
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp=exchange,
            quantity=1,
            trde_tp="00",
            price="1",
        )


@pytest.mark.parametrize("order_id", ["", "282", "00000028A", "../000000282"])
def test_rejects_non_nine_digit_order_id(order_id: str) -> None:
    from app.services.brokers.kiwoom.us_orders import validate_us_order_id

    with pytest.raises(KiwoomUsOrderRejected, match="nine digits"):
        validate_us_order_id(order_id)
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_us_orders.py -q
~~~

Expected: collection fails because us_orders.py and the US constants do not exist.

- [ ] **Step 3: Add US constants**

Append to app/services/brokers/kiwoom/constants.py:

~~~python
# US order API (/api/us/ordr)
US_ORDER_PATH = "/api/us/ordr"
US_ORDER_BUY_API_ID = "ust20000"
US_ORDER_SELL_API_ID = "ust20001"
US_ORDER_MODIFY_API_ID = "ust20002"
US_ORDER_CANCEL_API_ID = "ust20003"

# US account API (/api/us/acnt)
US_ACCOUNT_PATH = "/api/us/acnt"
US_ACCOUNT_OPEN_ORDERS_API_ID = "ust21050"
US_ACCOUNT_POSITIONS_API_ID = "ust21070"
US_ACCOUNT_TODAY_ORDERS_API_ID = "ust21510"
US_ACCOUNT_FOREIGN_DEPOSIT_API_ID = "ust21110"
US_ACCOUNT_DEPOSIT_DETAIL_API_ID = "ust21160"

US_STEX_TYPES = frozenset({"NA", "ND", "NY"})
US_EXCHANGE_TO_STEX = {
    "AMEX": "NA",
    "NASDAQ": "ND",
    "NASD": "ND",
    "NYSE": "NY",
}
~~~

- [ ] **Step 4: Create the US order client**

~~~python
"""Kiwoom US mock order payloads and transport calls."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.brokers.kiwoom import constants

BUY_TRADE_TYPES = frozenset({"00", "03", "26", "27", "30", "36", "37"})
SELL_TRADE_TYPES = BUY_TRADE_TYPES | frozenset({"33", "34", "35"})
PRICE_REQUIRED_TRADE_TYPES = frozenset({"00", "26", "27", "30", "34"})
STOP_REQUIRED_TRADE_TYPES = frozenset({"34", "35"})
_SYMBOL_RE = re.compile(r"^[A-Z0-9.]{1,12}$")
_ORDER_ID_RE = re.compile(r"^\d{9}$")


class KiwoomUsOrderRejected(ValueError):
    """Raised before transport when a US order request violates the contract."""


class _SupportsPostApi(Protocol):
    account_no: str

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]: ...


def validate_us_order_id(value: str) -> str:
    candidate = str(value or "").strip()
    if not _ORDER_ID_RE.fullmatch(candidate):
        raise KiwoomUsOrderRejected("Kiwoom US order id must be exactly nine digits")
    return candidate


def _symbol(value: str) -> str:
    candidate = str(value or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(candidate):
        raise KiwoomUsOrderRejected("Kiwoom US symbol must use DB dot format")
    return candidate


def _stex(value: str) -> str:
    candidate = str(value or "").strip().upper()
    if candidate not in constants.US_STEX_TYPES:
        raise KiwoomUsOrderRejected(f"unsupported Kiwoom US stex_tp={value!r}")
    return candidate


def _quantity(value: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError) as exc:
        raise KiwoomUsOrderRejected("quantity must be a positive integer") from exc
    if candidate <= 0:
        raise KiwoomUsOrderRejected("quantity must be a positive integer")
    return candidate


def _decimal_text(name: str, value: object) -> str:
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KiwoomUsOrderRejected(f"{name} must be a positive decimal") from exc
    if not candidate.is_finite() or candidate <= 0:
        raise KiwoomUsOrderRejected(f"{name} must be a positive decimal")
    return format(candidate, "f")


def build_us_place_order_body(
    *,
    side: str,
    symbol: str,
    stex_tp: str,
    quantity: int,
    trde_tp: str,
    price: object | None,
    stop_price: object | None = None,
) -> dict[str, str]:
    normalized_side = str(side).strip().lower()
    allowed = BUY_TRADE_TYPES if normalized_side == "buy" else SELL_TRADE_TYPES
    if normalized_side not in {"buy", "sell"} or trde_tp not in allowed:
        raise KiwoomUsOrderRejected(
            f"unsupported documented trde_tp={trde_tp!r} for side={side!r}"
        )
    if trde_tp in PRICE_REQUIRED_TRADE_TYPES:
        if price is None:
            raise KiwoomUsOrderRejected(f"trde_tp={trde_tp} requires price")
        order_price = _decimal_text("price", price)
    else:
        if price is not None:
            raise KiwoomUsOrderRejected(f"trde_tp={trde_tp} must omit price")
        order_price = ""

    body = {
        "stex_tp": _stex(stex_tp),
        "stk_cd": _symbol(symbol),
        "ord_qty": str(_quantity(quantity)),
        "ord_uv": order_price,
        "trde_tp": trde_tp,
    }
    if normalized_side == "sell":
        if trde_tp in STOP_REQUIRED_TRADE_TYPES:
            if stop_price is None:
                raise KiwoomUsOrderRejected(
                    f"trde_tp={trde_tp} requires stop_price"
                )
            body["stop_pric"] = _decimal_text("stop_price", stop_price)
        elif stop_price is not None:
            raise KiwoomUsOrderRejected(
                f"trde_tp={trde_tp} must omit stop_price"
            )
    return body


class KiwoomUsOrderClient:
    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ORDER_BUY_API_ID,
            path=constants.US_ORDER_PATH,
            body=build_us_place_order_body(side="buy", **kwargs),
        )

    async def place_sell_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ORDER_SELL_API_ID,
            path=constants.US_ORDER_PATH,
            body=build_us_place_order_body(side="sell", **kwargs),
        )

    async def modify_order(
        self,
        *,
        original_order_no: str,
        symbol: str,
        stex_tp: str,
        new_price: object,
        stop_price: object | None = None,
    ) -> dict[str, Any]:
        body = {
            "orig_ord_no": validate_us_order_id(original_order_no),
            "stex_tp": _stex(stex_tp),
            "stk_cd": _symbol(symbol),
            "mdfy_uv": _decimal_text("new_price", new_price),
        }
        if stop_price is not None:
            body["stop_pric"] = _decimal_text("stop_price", stop_price)
        return await self._client.post_api(
            api_id=constants.US_ORDER_MODIFY_API_ID,
            path=constants.US_ORDER_PATH,
            body=body,
        )

    async def cancel_order(
        self,
        *,
        original_order_no: str,
        symbol: str,
        stex_tp: str,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ORDER_CANCEL_API_ID,
            path=constants.US_ORDER_PATH,
            body={
                "orig_ord_no": validate_us_order_id(original_order_no),
                "stex_tp": _stex(stex_tp),
                "stk_cd": _symbol(symbol),
            },
        )
~~~

- [ ] **Step 5: Run tests and verify GREEN**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_us_orders.py -q
~~~

Expected: all tests pass.

- [ ] **Step 6: Commit**

~~~bash
git add app/services/brokers/kiwoom/constants.py app/services/brokers/kiwoom/us_orders.py tests/test_kiwoom_us_orders.py
git commit -m "feat(ROB-867): add Kiwoom US order client"
~~~

---

### Task 3: Implement US account reads and honest cash parsing

**Files:**
- Create: app/services/brokers/kiwoom/us_account.py
- Create: tests/test_kiwoom_us_account.py

**Interfaces:**
- Consumes: US account constants from Task 2 and guarded post_api transport.
- Produces: KiwoomUsAccountClient read methods; extract_usd_deposit(payload: dict[str, Any]) -> str | None.

- [ ] **Step 1: Write failing account TR and parser tests**

~~~python
from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.us_account import (
    KiwoomUsAccountClient,
    extract_usd_deposit,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.account_no = "US-MOCK"

    async def post_api(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"return_code": 0, "result_list": []}


@pytest.mark.asyncio
async def test_account_methods_use_proven_tr_ids_and_optional_filters() -> None:
    fake = FakeClient()
    account = KiwoomUsAccountClient(fake)

    await account.get_open_orders(
        side_code="2", stex_tp="ND", symbol="NVDA", cont_yn="Y", next_key="p2"
    )
    await account.get_positions(stex_tp="NY", symbol="TSM")
    await account.get_today_orders(side_code="0")
    await account.get_us_deposit_detail()

    assert fake.calls[0] == {
        "api_id": constants.US_ACCOUNT_OPEN_ORDERS_API_ID,
        "path": constants.US_ACCOUNT_PATH,
        "body": {"slby_tp": "2", "stex_tp": "ND", "stk_cd": "NVDA"},
        "cont_yn": "Y",
        "next_key": "p2",
    }
    assert fake.calls[1]["body"] == {"stex_tp": "NY", "stk_cd": "TSM"}
    assert fake.calls[2]["api_id"] == constants.US_ACCOUNT_TODAY_ORDERS_API_ID
    assert fake.calls[3]["api_id"] == constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID
    assert fake.calls[3]["body"] == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"d0_usd_fx_entr": "18042538.7700"}, "18042538.7700"),
        ({"d0_usd_fx_entr": "1,234.50"}, "1234.50"),
        ({"d0_usd_fx_entr": ""}, None),
        ({"d0_usd_fx_entr": "not-a-number"}, None),
        ({}, None),
    ],
)
def test_extract_usd_deposit_is_precise_and_fail_closed(
    payload: dict[str, Any], expected: str | None
) -> None:
    assert extract_usd_deposit(payload) == expected
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_us_account.py -q
~~~

Expected: collection fails because us_account.py does not exist.

- [ ] **Step 3: Create the account client**

~~~python
"""Kiwoom US mock account reads and deposit parsing."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.brokers.kiwoom import constants


class _SupportsPostApi(Protocol):
    account_no: str

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]: ...


def _optional_body(**values: str | None) -> dict[str, str]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def extract_usd_deposit(payload: dict[str, Any]) -> str | None:
    raw = payload.get("d0_usd_fx_entr")
    if raw in (None, ""):
        return None
    text = str(raw).replace(",", "").strip()
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return format(value, "f")


class KiwoomUsAccountClient:
    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def get_open_orders(
        self,
        *,
        order_date: str | None = None,
        side_code: str | None = None,
        stex_tp: str | None = None,
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_OPEN_ORDERS_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body=_optional_body(
                ord_dt=order_date,
                slby_tp=side_code,
                stex_tp=stex_tp,
                stk_cd=symbol,
            ),
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_positions(
        self,
        *,
        stex_tp: str | None = None,
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_POSITIONS_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body=_optional_body(stex_tp=stex_tp, stk_cd=symbol),
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_today_orders(
        self,
        *,
        side_code: str | None = None,
        stex_tp: str | None = None,
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_TODAY_ORDERS_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body=_optional_body(
                slby_tp=side_code,
                stex_tp=stex_tp,
                stk_cd=symbol,
            ),
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_foreign_deposit(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_FOREIGN_DEPOSIT_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body={},
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_us_deposit_detail(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body={},
            cont_yn=cont_yn,
            next_key=next_key,
        )
~~~

- [ ] **Step 4: Run tests and verify GREEN**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_us_account.py -q
~~~

Expected: all tests pass.

- [ ] **Step 5: Commit**

~~~bash
git add app/services/brokers/kiwoom/us_account.py tests/test_kiwoom_us_account.py
git commit -m "feat(ROB-867): add Kiwoom US account reads"
~~~

---

### Task 4: Share fail-closed broker response shaping

**Files:**
- Create: app/mcp_server/tooling/orders_kiwoom_shared.py
- Modify: app/mcp_server/tooling/orders_kiwoom_variants.py
- Create: tests/test_mcp_kiwoom_shared.py
- Test: tests/test_mcp_kiwoom_order_variants.py

**Interfaces:**
- Consumes: constants.SUCCESS_RETURN_CODE.
- Produces: derive_broker_success(payload) -> bool; finalize_broker_response(base, payload) -> dict[str, Any].

- [ ] **Step 1: Write failing shared-helper tests**

~~~python
from app.mcp_server.tooling.orders_kiwoom_shared import (
    derive_broker_success,
    finalize_broker_response,
)


def test_success_requires_explicit_zero_return_code() -> None:
    assert derive_broker_success({"return_code": 0}) is True
    assert derive_broker_success({"return_code": "0"}) is True
    assert derive_broker_success({}) is False
    assert derive_broker_success({"return_code": None}) is False
    assert derive_broker_success({"return_code": 20}) is False


def test_rc9000_is_classified_without_losing_raw_evidence() -> None:
    raw = {
        "return_code": 20,
        "return_msg": "[2000](RC9000:모의투자에서는 해당업무가 제공되지 않습니다.)",
    }
    result = finalize_broker_response({"source": "kiwoom"}, raw)
    assert result["success"] is False
    assert result["error_code"] == "capability_unsupported"
    assert result["broker_response"] is raw
    assert result["return_msg"] == raw["return_msg"]
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_mcp_kiwoom_shared.py -q
~~~

Expected: collection fails because orders_kiwoom_shared.py does not exist.

- [ ] **Step 3: Create the shared response module**

~~~python
"""Shared fail-closed response shaping for Kiwoom MCP namespaces."""

from __future__ import annotations

from typing import Any

from app.services.brokers.kiwoom import constants

_PASSTHROUGH_KEYS = (
    "return_code",
    "return_msg",
    "continuation",
    "ord_no",
    "order_no",
)


def derive_broker_success(broker_response: dict[str, Any]) -> bool:
    if "return_code" not in broker_response:
        return False
    value = broker_response["return_code"]
    if value is None:
        return False
    try:
        return int(value) == constants.SUCCESS_RETURN_CODE
    except (TypeError, ValueError):
        return False


def _capability_error_code(broker_response: dict[str, Any]) -> str | None:
    message = str(broker_response.get("return_msg") or "")
    try:
        code = int(broker_response.get("return_code"))
    except (TypeError, ValueError):
        return None
    if code == 20 and (
        "RC9000" in message or "모의투자에서는 해당업무가 제공되지 않습니다" in message
    ):
        return "capability_unsupported"
    return None


def finalize_broker_response(
    base: dict[str, Any], broker_response: dict[str, Any]
) -> dict[str, Any]:
    response = {
        "success": derive_broker_success(broker_response),
        **base,
        "broker_response": broker_response,
    }
    for key in _PASSTHROUGH_KEYS:
        if key in broker_response:
            response[key] = broker_response[key]
    if error_code := _capability_error_code(broker_response):
        response["error_code"] = error_code
    return response
~~~

- [ ] **Step 4: Replace KR-local implementations with compatibility aliases**

In app/mcp_server/tooling/orders_kiwoom_variants.py, import:

~~~python
from app.mcp_server.tooling.orders_kiwoom_shared import (
    derive_broker_success as _derive_broker_success,
)
from app.mcp_server.tooling.orders_kiwoom_shared import (
    finalize_broker_response as _finalize_broker_response,
)
~~~

Delete the local _MUTATION_PASSTHROUGH_KEYS, _derive_broker_success, and
_finalize_broker_response definitions. Keep the imported aliases so the
existing direct test of mod._derive_broker_success remains valid.

- [ ] **Step 5: Run shared and KR regression tests**

Run:

~~~bash
uv run --all-groups pytest tests/test_mcp_kiwoom_shared.py tests/test_mcp_kiwoom_order_variants.py -q
~~~

Expected: all tests pass with unchanged KR contracts.

- [ ] **Step 6: Commit**

~~~bash
git add app/mcp_server/tooling/orders_kiwoom_shared.py app/mcp_server/tooling/orders_kiwoom_variants.py tests/test_mcp_kiwoom_shared.py
git commit -m "refactor(ROB-867): share Kiwoom broker response shaping"
~~~

---

### Task 5: Add seven Kiwoom mock US MCP tools

**Files:**
- Create: app/mcp_server/tooling/orders_kiwoom_us_variants.py
- Create: tests/test_mcp_kiwoom_us_order_variants.py

**Interfaces:**
- Consumes: KiwoomMockUsClient, KiwoomUsOrderClient, KiwoomUsAccountClient, get_us_exchange_by_symbol, shared response shaping.
- Produces: KIWOOM_MOCK_US_READ_TOOL_NAMES, KIWOOM_MOCK_US_MUTATION_TOOL_NAMES, KIWOOM_MOCK_US_TOOL_NAMES, register(mcp).

- [ ] **Step 1: Write failing MCP contract tests**

Create a DummyMCP registrar and add these tests:

~~~python
from __future__ import annotations

from typing import Any

import pytest


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def _tools() -> dict[str, Any]:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    mcp = DummyMCP()
    module.register(mcp)
    return mcp.tools


def test_registers_exact_seven_us_tools() -> None:
    from app.mcp_server.tooling.orders_kiwoom_us_variants import (
        KIWOOM_MOCK_US_TOOL_NAMES,
    )

    assert set(_tools()) == KIWOOM_MOCK_US_TOOL_NAMES
    assert len(KIWOOM_MOCK_US_TOOL_NAMES) == 7


@pytest.mark.asyncio
async def test_rejects_advanced_trde_tp_before_lookup_or_client(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"lookup": 0, "client": 0}

    async def fake_lookup(symbol: str) -> str:
        calls["lookup"] += 1
        return "NASD"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)

    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=200.0,
        trde_tp="26",
        dry_run=False,
        confirm=True,
    )

    assert result["success"] is False
    assert result["error_code"] == "unsupported_trde_tp"
    assert result["supported_trde_tp"] == ["00", "03"]
    assert calls == {"lookup": 0, "client": 0}


@pytest.mark.asyncio
async def test_limit_requires_price_and_market_rejects_price(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    limit = await _tools()["kiwoom_mock_us_preview_order"](
        symbol="NVDA", side="buy", quantity=1, trde_tp="00", price=None
    )
    market = await _tools()["kiwoom_mock_us_preview_order"](
        symbol="NVDA", side="buy", quantity=1, trde_tp="03", price=1.0
    )
    assert limit["success"] is False
    assert market["success"] is False


@pytest.mark.asyncio
async def test_confirmed_limit_resolves_exchange_and_calls_broker(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls: list[dict[str, Any]] = []

    async def fake_lookup(symbol: str) -> str:
        assert symbol == "NVDA"
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            calls.append({"client": type(client).__name__})

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"return_code": 0, "ord_no": "000000282"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsOrderClient", FakeOrders)

    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=213.04,
        trde_tp="00",
        dry_run=False,
        confirm=True,
    )

    assert result["success"] is True
    assert result["account_mode"] == "kiwoom_mock_us"
    assert calls[-1]["stex_tp"] == "ND"
    assert calls[-1]["trde_tp"] == "00"


@pytest.mark.asyncio
async def test_orderable_cash_is_labeled_as_deposit(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            pass

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            return {"return_code": 0, "d0_usd_fx_entr": "1234.5000"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsAccountClient", FakeAccount)

    result = await _tools()["kiwoom_mock_us_get_orderable_cash"]()
    assert result["cash"] == "1234.5000"
    assert result["currency"] == "USD"
    assert result["cash_semantics"] == "deposit_not_broker_orderable"
    assert result["orderable_quantity_supported"] is False


@pytest.mark.asyncio
async def test_history_scope_selects_open_or_today(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls: list[str] = []

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            pass

        async def get_open_orders(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("open")
            return {"return_code": 0, "result_list": []}

        async def get_today_orders(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("today")
            return {"return_code": 0, "result_list": []}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsAccountClient", FakeAccount)

    tools = _tools()
    await tools["kiwoom_mock_us_get_order_history"](scope="open")
    await tools["kiwoom_mock_us_get_order_history"](scope="today")
    assert calls == ["open", "today"]
~~~

Add these exact guard and passthrough tests to the same file:

~~~python
@pytest.mark.asyncio
async def test_disabled_config_reports_only_us_keys(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    monkeypatch.setattr(
        module,
        "validate_kiwoom_mock_us_config",
        lambda: ["KIWOOM_MOCK_US_APP_KEY"],
    )
    result = await _tools()["kiwoom_mock_us_get_positions"]()
    assert result["success"] is False
    assert "KIWOOM_MOCK_US_APP_KEY" in result["error"]
    assert "KIWOOM_MOCK_APP_KEY" not in result["error"]


@pytest.mark.asyncio
async def test_live_place_without_confirm_never_constructs_client(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"client": 0}

    async def fake_lookup(symbol: str) -> str:
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    result = await _tools()["kiwoom_mock_us_place_order"](
        symbol="NVDA",
        side="buy",
        quantity=1,
        price=1.0,
        trde_tp="00",
        dry_run=False,
        confirm=False,
    )
    assert result["success"] is False
    assert calls["client"] == 0


@pytest.mark.asyncio
async def test_unsupported_exchange_and_unsafe_id_stop_before_client(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls = {"client": 0}

    async def fake_lookup(symbol: str) -> str:
        return "OTC"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    preview = await _tools()["kiwoom_mock_us_preview_order"](
        symbol="OTCM",
        side="buy",
        quantity=1,
        price=1.0,
        trde_tp="00",
    )
    cancel = await _tools()["kiwoom_mock_us_cancel_order"](
        order_id="../282",
        symbol="NVDA",
        dry_run=False,
        confirm=True,
    )
    assert preview["success"] is False
    assert cancel["success"] is False
    assert calls["client"] == 0


@pytest.mark.asyncio
async def test_modify_and_cancel_do_not_invent_quantity(monkeypatch) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    calls: list[dict[str, Any]] = []

    async def fake_lookup(symbol: str) -> str:
        return "NYSE"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            pass

        async def modify_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"return_code": 0, "ord_no": "000000284"}

        async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"return_code": 0, "ord_no": "000000285"}

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsOrderClient", FakeOrders)
    tools = _tools()
    await tools["kiwoom_mock_us_modify_order"](
        order_id="000000282",
        symbol="TSM",
        new_price=100.0,
        dry_run=False,
        confirm=True,
    )
    await tools["kiwoom_mock_us_cancel_order"](
        order_id="000000284",
        symbol="TSM",
        dry_run=False,
        confirm=True,
    )
    assert all("quantity" not in call for call in calls)
    assert calls[0]["new_price"] == 100.0


@pytest.mark.asyncio
async def test_unparseable_deposit_is_null_and_nonzero_is_failure(
    monkeypatch,
) -> None:
    from app.mcp_server.tooling import orders_kiwoom_us_variants as module

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            pass

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            return {
                "return_code": 20,
                "return_msg": "RC9000",
                "d0_usd_fx_entr": "invalid",
            }

    monkeypatch.setattr(module, "_mock_us_config_error", lambda: None)
    monkeypatch.setattr(module, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(module, "KiwoomUsAccountClient", FakeAccount)
    result = await _tools()["kiwoom_mock_us_get_orderable_cash"]()
    assert result["success"] is False
    assert result["cash"] is None
    assert result["cash_source"] == "ust21160.d0_usd_fx_entr_unparsed"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_mcp_kiwoom_us_order_variants.py -q
~~~

Expected: collection fails because orders_kiwoom_us_variants.py does not exist.

- [ ] **Step 3: Implement module constants and guards**

~~~python
ACCOUNT_MODE_KIWOOM_MOCK_US = "kiwoom_mock_us"
SUPPORTED_MCP_TRDE_TYPES = ("00", "03")

KIWOOM_MOCK_US_READ_TOOL_NAMES = {
    "kiwoom_mock_us_get_order_history",
    "kiwoom_mock_us_get_positions",
    "kiwoom_mock_us_get_orderable_cash",
}
KIWOOM_MOCK_US_MUTATION_TOOL_NAMES = {
    "kiwoom_mock_us_preview_order",
    "kiwoom_mock_us_place_order",
    "kiwoom_mock_us_modify_order",
    "kiwoom_mock_us_cancel_order",
}
KIWOOM_MOCK_US_TOOL_NAMES = (
    KIWOOM_MOCK_US_READ_TOOL_NAMES | KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
)


def _mock_us_config_error() -> dict[str, Any] | None:
    missing = validate_kiwoom_mock_us_config()
    if not missing:
        return None
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "error": (
            "Kiwoom US mock account is disabled or missing required "
            "configuration: " + ", ".join(missing)
        ),
    }


def _trade_type_error(trde_tp: str) -> dict[str, Any] | None:
    if trde_tp in SUPPORTED_MCP_TRDE_TYPES:
        return None
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "error_code": "unsupported_trde_tp",
        "rejected_trde_tp": trde_tp,
        "supported_trde_tp": list(SUPPORTED_MCP_TRDE_TYPES),
        "error": f"kiwoom_mock_us does not expose trde_tp={trde_tp!r}.",
    }


def _price_error(trde_tp: str, price: float | None) -> dict[str, Any] | None:
    if trde_tp == "00" and (price is None or price <= 0):
        return {
            "success": False,
            "error": "trde_tp='00' requires price > 0.",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        }
    if trde_tp == "03" and price is not None:
        return {
            "success": False,
            "error": "trde_tp='03' must omit price.",
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        }
    return None


async def _resolve_stex(symbol: str) -> str:
    exchange = str(await get_us_exchange_by_symbol(symbol)).strip().upper()
    try:
        return constants.US_EXCHANGE_TO_STEX[exchange]
    except KeyError as exc:
        raise ValueError(
            f"Kiwoom US mock rejects unsupported exchange={exchange!r}"
        ) from exc
~~~

- [ ] **Step 4: Implement exact tool signatures and dispatch**

The register function must expose these signatures:

~~~python
def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="kiwoom_mock_us_preview_order",
        description="Preview a Kiwoom US mock order; MCP supports trde_tp 00/03.",
    )
    async def preview(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: float | None = None,
        trde_tp: str = "00",
        market: str = "us",
    ) -> dict[str, Any]:
        for guard in (
            _mock_us_config_error(),
            _trade_type_error(trde_tp),
            _price_error(trde_tp, price),
        ):
            if guard:
                return guard
        if market.strip().lower() != "us" or quantity <= 0:
            return {
                "success": False,
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
                "error": "kiwoom_mock_us requires market='us' and quantity > 0.",
            }
        try:
            stex_tp = await _resolve_stex(symbol)
            body = build_us_place_order_body(
                side=side,
                symbol=symbol,
                stex_tp=stex_tp,
                quantity=quantity,
                trde_tp=trde_tp,
                price=price,
            )
        except Exception as exc:
            return _exception_response("preview_order", exc)
        return {
            "success": True,
            "preview": True,
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "symbol": symbol.strip().upper(),
            "side": side,
            "quantity": quantity,
            "price": price,
            "trde_tp": trde_tp,
            "stex_tp": stex_tp,
            "request_body": body,
        }

    @mcp.tool(
        name="kiwoom_mock_us_place_order",
        description="Place a Kiwoom US mock order; dry_run defaults to true.",
    )
    async def place(
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: int,
        price: float | None = None,
        trde_tp: str = "00",
        market: str = "us",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        preview_result = await preview(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            trde_tp=trde_tp,
            market=market,
        )
        if not preview_result.get("success") or dry_run:
            return {**preview_result, "dry_run": dry_run}
        if not confirm:
            return {
                "success": False,
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
                "error": (
                    "kiwoom_mock_us_place_order requires confirm=True "
                    "when dry_run=False."
                ),
            }
        try:
            client = KiwoomMockUsClient.from_app_settings()
            orders = KiwoomUsOrderClient(cast(Any, client))
            kwargs = {
                "symbol": symbol,
                "stex_tp": preview_result["stex_tp"],
                "quantity": quantity,
                "trde_tp": trde_tp,
                "price": price,
            }
            if side == "buy":
                raw = await orders.place_buy_order(**kwargs)
            else:
                raw = await orders.place_sell_order(**kwargs)
        except Exception as exc:
            return _exception_response("place_order", exc)
        return finalize_broker_response(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
                "dry_run": False,
                "symbol": symbol.strip().upper(),
                "side": side,
                "quantity": quantity,
                "price": price,
                "trde_tp": trde_tp,
                "stex_tp": preview_result["stex_tp"],
            },
            raw,
        )

    @mcp.tool(name="kiwoom_mock_us_modify_order")
    async def modify(
        order_id: str,
        symbol: str,
        new_price: float,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            validate_us_order_id(order_id)
            if new_price <= 0:
                raise ValueError("new_price must be > 0")
            stex_tp = await _resolve_stex(symbol)
        except Exception as exc:
            return _exception_response("modify_order", exc)
        base = {
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "dry_run": dry_run,
            "order_id": order_id,
            "symbol": symbol.strip().upper(),
            "new_price": new_price,
            "stex_tp": stex_tp,
        }
        if dry_run:
            return {"success": True, **base}
        if not confirm:
            return {
                "success": False,
                **base,
                "error": (
                    "kiwoom_mock_us_modify_order requires confirm=True "
                    "when dry_run=False."
                ),
            }
        try:
            client = KiwoomMockUsClient.from_app_settings()
            raw = await KiwoomUsOrderClient(cast(Any, client)).modify_order(
                original_order_no=order_id,
                symbol=symbol,
                stex_tp=stex_tp,
                new_price=new_price,
            )
        except Exception as exc:
            return _exception_response("modify_order", exc)
        return finalize_broker_response(base, raw)

    @mcp.tool(name="kiwoom_mock_us_cancel_order")
    async def cancel(
        order_id: str,
        symbol: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            validate_us_order_id(order_id)
            stex_tp = await _resolve_stex(symbol)
        except Exception as exc:
            return _exception_response("cancel_order", exc)
        base = {
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            "dry_run": dry_run,
            "order_id": order_id,
            "symbol": symbol.strip().upper(),
            "stex_tp": stex_tp,
        }
        if dry_run:
            return {"success": True, **base}
        if not confirm:
            return {
                "success": False,
                **base,
                "error": (
                    "kiwoom_mock_us_cancel_order requires confirm=True "
                    "when dry_run=False."
                ),
            }
        try:
            client = KiwoomMockUsClient.from_app_settings()
            raw = await KiwoomUsOrderClient(cast(Any, client)).cancel_order(
                original_order_no=order_id,
                symbol=symbol,
                stex_tp=stex_tp,
            )
        except Exception as exc:
            return _exception_response("cancel_order", exc)
        return finalize_broker_response(base, raw)

    @mcp.tool(name="kiwoom_mock_us_get_order_history")
    async def history(
        scope: Literal["open", "today"] = "open",
        symbol: str | None = None,
        side_code: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            stex_tp = await _resolve_stex(symbol) if symbol else None
            client = KiwoomMockUsClient.from_app_settings()
            account = KiwoomUsAccountClient(cast(Any, client))
            method = (
                account.get_open_orders
                if scope == "open"
                else account.get_today_orders
            )
            raw = await method(
                side_code=side_code,
                stex_tp=stex_tp,
                symbol=symbol,
                cont_yn=cont_yn,
                next_key=next_key,
            )
        except Exception as exc:
            return _exception_response("get_order_history", exc)
        return finalize_broker_response(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
                "scope": scope,
            },
            raw,
        )

    @mcp.tool(name="kiwoom_mock_us_get_positions")
    async def positions(
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            stex_tp = await _resolve_stex(symbol) if symbol else None
            client = KiwoomMockUsClient.from_app_settings()
            raw = await KiwoomUsAccountClient(cast(Any, client)).get_positions(
                stex_tp=stex_tp,
                symbol=symbol,
                cont_yn=cont_yn,
                next_key=next_key,
            )
        except Exception as exc:
            return _exception_response("get_positions", exc)
        return finalize_broker_response(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            },
            raw,
        )

    @mcp.tool(name="kiwoom_mock_us_get_orderable_cash")
    async def cash() -> dict[str, Any]:
        if guard := _mock_us_config_error():
            return guard
        try:
            client = KiwoomMockUsClient.from_app_settings()
            raw = await KiwoomUsAccountClient(
                cast(Any, client)
            ).get_us_deposit_detail()
        except Exception as exc:
            return _exception_response("get_orderable_cash", exc)
        value = extract_usd_deposit(raw)
        result = finalize_broker_response(
            {
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
            },
            raw,
        )
        result.update(
            {
                "cash": value,
                "currency": "USD",
                "cash_source": (
                    "ust21160.d0_usd_fx_entr"
                    if value is not None
                    else "ust21160.d0_usd_fx_entr_unparsed"
                ),
                "cash_semantics": "deposit_not_broker_orderable",
                "orderable_quantity_supported": False,
                "warning": (
                    "Kiwoom mock rejects ust31490; cash is USD deposit "
                    "evidence, not per-symbol broker orderable cash."
                ),
            }
        )
        return result
~~~

The module-level imports and exception helper are:

~~~python
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from app.core.config import validate_kiwoom_mock_us_config
from app.mcp_server.tooling.orders_kiwoom_shared import finalize_broker_response
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.us_account import (
    KiwoomUsAccountClient,
    extract_usd_deposit,
)
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient
from app.services.brokers.kiwoom.us_orders import (
    KiwoomUsOrderClient,
    build_us_place_order_body,
    validate_us_order_id,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _exception_response(operation: str, exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK_US,
        "error": (
            f"kiwoom_mock_us_{operation} failed: "
            f"{type(exc).__name__}: {exc}"
        ),
    }
~~~

- [ ] **Step 5: Run MCP tests and verify GREEN**

Run:

~~~bash
uv run --all-groups pytest tests/test_mcp_kiwoom_us_order_variants.py tests/test_mcp_kiwoom_shared.py -q
~~~

Expected: all tests pass.

- [ ] **Step 6: Commit**

~~~bash
git add app/mcp_server/tooling/orders_kiwoom_us_variants.py tests/test_mcp_kiwoom_us_order_variants.py
git commit -m "feat(ROB-867): expose guarded Kiwoom mock US tools"
~~~

---

### Task 6: Register and govern the new namespace

**Files:**
- Modify: app/mcp_server/tooling/registry.py
- Modify: app/mcp_server/tooling/route_request_lanes.py
- Modify: app/mcp_server/tooling/account_read_registration.py
- Modify: app/mcp_server/tooling/analysis_readonly_registration.py
- Modify: .claude/settings.readonly.json
- Modify: tests/test_mcp_profiles.py
- Modify: tests/test_route_request_registry_diff.py
- Modify: tests/test_watch_triage_readonly_settings.py

**Interfaces:**
- Consumes: US tool-name sets from Task 5.
- Produces: correct DEFAULT/KIWOOM registration and exhaustive readonly/route classification.

- [ ] **Step 1: Write failing profile and governance assertions**

In tests/test_mcp_profiles.py import KIWOOM_MOCK_US_TOOL_NAMES and add:

~~~python
class TestKiwoomUsProfile:
    def test_registers_us_namespace_in_kiwoom_profile(self) -> None:
        mcp = _build_mcp(McpProfile.KIWOOM)
        assert KIWOOM_MOCK_US_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_us_namespace_in_default_when_enabled(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "kiwoom_mock_us_enabled", True)
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIWOOM_MOCK_US_TOOL_NAMES <= mcp.tools.keys()

    def test_omits_us_namespace_in_default_when_disabled(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "kiwoom_mock_us_enabled", False)
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIWOOM_MOCK_US_TOOL_NAMES.isdisjoint(mcp.tools.keys())
~~~

Extend the KIWOOM order matrix:

~~~python
    McpProfile.KIWOOM: set(
        KIWOOM_MOCK_TOOL_NAMES | KIWOOM_MOCK_US_TOOL_NAMES
    ),
~~~

In tests/test_watch_triage_readonly_settings.py add these names to
KNOWN_MUTATION_TOOLS:

~~~python
        "kiwoom_mock_us_preview_order",
        "kiwoom_mock_us_place_order",
        "kiwoom_mock_us_modify_order",
        "kiwoom_mock_us_cancel_order",
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_mcp_profiles.py tests/test_route_request_registry_diff.py tests/test_watch_triage_readonly_settings.py -q
~~~

Expected: the US namespace is unregistered/unclassified and missing from the deny-list.

- [ ] **Step 3: Register DEFAULT and KIWOOM surfaces**

Import orders_kiwoom_us_variants in registry.py and add:

~~~python
        if settings.kiwoom_mock_us_enabled:
            orders_kiwoom_us_variants.register(mcp)
~~~

to the DEFAULT profile after the KR gate. Change the KIWOOM branch to:

~~~python
    elif profile is McpProfile.KIWOOM:
        orders_kiwoom_variants.register(mcp)
        orders_kiwoom_us_variants.register(mcp)
~~~

- [ ] **Step 4: Add route and restricted-profile classification**

Import all three US name sets where needed. In route_request_lanes.py:

~~~python
    | KIWOOM_MOCK_US_MUTATION_TOOL_NAMES
~~~

goes into MUTATION_TOOLS, and:

~~~python
        *KIWOOM_MOCK_US_READ_TOOL_NAMES,
~~~

goes into READ_ONLY_ADVISORY_TOOLS.

In tests/test_route_request_registry_diff.py add the flag-gated read names:

~~~python
        "kiwoom_mock_us_get_order_history",
        "kiwoom_mock_us_get_positions",
        "kiwoom_mock_us_get_orderable_cash",
~~~

to _FLAG_GATED_OR_OPTIONAL.

Union KIWOOM_MOCK_US_TOOL_NAMES into both
ACCOUNT_READ_FORBIDDEN_TOOL_NAMES and ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES.

- [ ] **Step 5: Add exact readonly deny entries**

Append to .claude/settings.readonly.json permissions.deny:

~~~json
      "mcp__auto_trader_local__kiwoom_mock_us_preview_order",
      "mcp__auto_trader_local__kiwoom_mock_us_place_order",
      "mcp__auto_trader_local__kiwoom_mock_us_modify_order",
      "mcp__auto_trader_local__kiwoom_mock_us_cancel_order"
~~~

- [ ] **Step 6: Run profile/governance tests and verify GREEN**

Run:

~~~bash
uv run --all-groups pytest tests/test_mcp_profiles.py tests/test_route_request_lanes.py tests/test_route_request_registry_diff.py tests/test_watch_triage_readonly_settings.py -q
~~~

Expected: all tests pass and every flag-enabled DEFAULT tool is classified.

- [ ] **Step 7: Commit**

~~~bash
git add app/mcp_server/tooling/registry.py app/mcp_server/tooling/route_request_lanes.py app/mcp_server/tooling/account_read_registration.py app/mcp_server/tooling/analysis_readonly_registration.py .claude/settings.readonly.json tests/test_mcp_profiles.py tests/test_route_request_registry_diff.py tests/test_watch_triage_readonly_settings.py
git commit -m "feat(ROB-867): register and govern Kiwoom mock US tools"
~~~

---

### Task 7: Add operator-safe US smoke CLI

**Files:**
- Create: scripts/kiwoom_mock_us_smoke.py
- Create: tests/test_kiwoom_mock_us_smoke_cli.py

**Interfaces:**
- Consumes: registered US MCP tools for normal modes and low-level order client for advanced probes.
- Produces: preflight/preview/full CLI modes plus double-confirmed order-type probe.

- [ ] **Step 1: Write failing CLI safety tests**

~~~python
from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest

from scripts import kiwoom_mock_us_smoke as smoke


def test_parser_defaults_are_non_mutating() -> None:
    args = smoke.build_parser().parse_args(["--mode", "preflight"])
    assert args.confirm is False
    assert args.probe_order_types is None
    assert args.confirm_probes is False


@pytest.mark.asyncio
async def test_preflight_reports_only_missing_key_names(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "validate_kiwoom_mock_us_config",
        lambda: ["KIWOOM_MOCK_US_APP_KEY"],
    )
    result = await smoke.run_preflight()
    assert result == {
        "step": "preflight",
        "ok": False,
        "missing_env_keys": ["KIWOOM_MOCK_US_APP_KEY"],
    }


@pytest.mark.asyncio
async def test_complete_preflight_calls_all_read_trs(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccount:
        def __init__(self, client: Any) -> None:
            pass

        async def get_open_orders(self) -> dict[str, Any]:
            calls.append("ust21050")
            return {"return_code": 0}

        async def get_positions(self) -> dict[str, Any]:
            calls.append("ust21070")
            return {"return_code": 0}

        async def get_today_orders(self) -> dict[str, Any]:
            calls.append("ust21510")
            return {"return_code": 0}

        async def get_foreign_deposit(self) -> dict[str, Any]:
            calls.append("ust21110")
            return {"return_code": 0}

        async def get_us_deposit_detail(self) -> dict[str, Any]:
            calls.append("ust21160")
            return {"return_code": 0}

    monkeypatch.setattr(smoke, "validate_kiwoom_mock_us_config", lambda: [])
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsAccountClient", FakeAccount)
    result = await smoke.run_preflight()
    assert result["ok"] is True
    assert calls == ["ust21050", "ust21070", "ust21510", "ust21110", "ust21160"]


@pytest.mark.asyncio
async def test_probe_requires_second_confirmation_before_client(monkeypatch) -> None:
    calls = {"client": 0}

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            calls["client"] += 1
            return cls()

    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    args = Namespace(
        confirm_probes=False,
        probe_order_types="26",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )
    with pytest.raises(smoke.SmokeRejected, match="confirm-probes"):
        await smoke.run_probe(args)
    assert calls["client"] == 0


@pytest.mark.asyncio
async def test_full_always_cancels_accepted_order(monkeypatch) -> None:
    calls: list[str] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        calls.append("place-live" if not kwargs["dry_run"] else "place-dry")
        if kwargs["dry_run"]:
            return {"success": True}
        return {"success": True, "ord_no": "000000282"}

    async def history(**kwargs: Any) -> dict[str, Any]:
        calls.append("history")
        return {"success": True, "result_list": []}

    async def cancel(**kwargs: Any) -> dict[str, Any]:
        calls.append("cancel")
        return {"success": True, "ord_no": "000000283"}

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
            "kiwoom_mock_us_get_order_history": history,
            "kiwoom_mock_us_cancel_order": cancel,
            "kiwoom_mock_us_get_positions": history,
        },
    )
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--price",
            "1.00",
            "--confirm",
        ]
    )
    assert await smoke.run_full(args) == 0
    assert calls.index("cancel") > calls.index("place-live")


def test_extract_order_id_accepts_only_nine_digits() -> None:
    assert smoke.extract_order_id({"ord_no": "000000282"}) == "000000282"
    assert smoke.extract_order_id({"ord_no": "282"}) is None
    assert smoke.extract_order_id({"ord_no": "../000000282"}) is None
~~~

Add these exact CLI regression tests:

~~~python
@pytest.mark.asyncio
async def test_full_stops_after_dry_run_without_confirm(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def preview(**kwargs: Any) -> dict[str, Any]:
        return {"success": True}

    async def place(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(
        smoke,
        "_tools",
        lambda: {
            "kiwoom_mock_us_preview_order": preview,
            "kiwoom_mock_us_place_order": place,
        },
    )
    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "NVDA",
            "--quantity",
            "1",
            "--price",
            "1.00",
        ]
    )
    assert await smoke.run_full(args) == 0
    assert calls == [
        {
            "symbol": "NVDA",
            "side": "buy",
            "quantity": 1,
            "price": 1.0,
            "trde_tp": "00",
            "dry_run": True,
        }
    ]


def test_parse_probe_codes_is_ordered_and_deduplicated() -> None:
    assert smoke.parse_probe_codes("26,27,26,30") == ("26", "27", "30")
    assert smoke.parse_probe_codes(None) == ()


@pytest.mark.asyncio
async def test_probe_cancels_every_accepted_order(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_lookup(symbol: str) -> str:
        return "NASDAQ"

    class FakeClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeOrders:
        def __init__(self, client: Any) -> None:
            pass

        async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("place")
            return {"return_code": 0, "ord_no": "000000282"}

        async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("cancel")
            return {"return_code": 0, "ord_no": "000000283"}

    monkeypatch.setattr(smoke, "get_us_exchange_by_symbol", fake_lookup)
    monkeypatch.setattr(smoke, "KiwoomMockUsClient", FakeClient)
    monkeypatch.setattr(smoke, "KiwoomUsOrderClient", FakeOrders)
    args = Namespace(
        confirm_probes=True,
        probe_order_types="26",
        probe_side="buy",
        symbol="NVDA",
        quantity=1,
        price=1.0,
        stop_price=None,
    )
    assert await smoke.run_probe(args) == 0
    assert calls == ["place", "cancel"]


def test_emit_does_not_add_sensitive_values(capsys) -> None:
    smoke._emit({"step": "preflight", "missing_env_keys": ["KIWOOM_MOCK_US_APP_KEY"]})
    rendered = capsys.readouterr().out
    assert "KIWOOM_MOCK_US_APP_KEY" in rendered
    assert "SECRET-FIXTURE" not in rendered
    assert "TOKEN-FIXTURE" not in rendered
    assert "ACCOUNT-FIXTURE" not in rendered
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_mock_us_smoke_cli.py -q
~~~

Expected: collection fails because scripts/kiwoom_mock_us_smoke.py does not exist.

- [ ] **Step 3: Implement non-mutating helpers and preflight**

~~~python
"""Operator-safe Kiwoom US mock lifecycle smoke (ROB-867)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

from app.core.config import validate_kiwoom_mock_us_config
from app.mcp_server.tooling import orders_kiwoom_us_variants as us_variants
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.us_account import KiwoomUsAccountClient
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient
from app.services.brokers.kiwoom.us_orders import KiwoomUsOrderClient
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

_ORDER_ID_RE = re.compile(r"^\d{9}$")


class SmokeRejected(RuntimeError):
    """Raised when operator input violates a smoke safety boundary."""


class _Recorder:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def _tools() -> dict[str, Any]:
    recorder = _Recorder()
    us_variants.register(recorder)
    return recorder.tools


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def extract_order_id(payload: dict[str, Any]) -> str | None:
    for key in ("ord_no", "order_no"):
        value = str(payload.get(key) or "").strip()
        if _ORDER_ID_RE.fullmatch(value):
            return value
    return None


def parse_probe_codes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))


async def run_preflight() -> dict[str, Any]:
    missing = validate_kiwoom_mock_us_config()
    if missing:
        return {
            "step": "preflight",
            "ok": False,
            "missing_env_keys": missing,
        }
    client = KiwoomMockUsClient.from_app_settings()
    account = KiwoomUsAccountClient(client)
    checks = {
        "ust21050": await account.get_open_orders(),
        "ust21070": await account.get_positions(),
        "ust21510": await account.get_today_orders(),
        "ust21110": await account.get_foreign_deposit(),
        "ust21160": await account.get_us_deposit_detail(),
    }
    return {
        "step": "preflight",
        "ok": all(
            int(payload.get("return_code", -1)) == 0
            for payload in checks.values()
        ),
        "missing_env_keys": [],
        "checks": checks,
    }
~~~

- [ ] **Step 4: Implement full lifecycle and probe cleanup**

~~~python
async def run_preview(args: argparse.Namespace) -> dict[str, Any]:
    return await _tools()["kiwoom_mock_us_preview_order"](
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        trde_tp=args.trde_tp,
    )


async def run_full(args: argparse.Namespace) -> int:
    if args.trde_tp != "00":
        raise SmokeRejected("full mode is limit-only; use trde_tp=00")
    tools = _tools()
    preview = await run_preview(args)
    _emit({"step": "preview", **preview})
    if not preview.get("success"):
        return 2

    dry = await tools["kiwoom_mock_us_place_order"](
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        trde_tp="00",
        dry_run=True,
    )
    _emit({"step": "place_dry_run", **dry})
    if not dry.get("success"):
        return 2
    if not args.confirm:
        _emit({"step": "stop", "reason": "no --confirm; no broker mutation"})
        return 0

    placed = await tools["kiwoom_mock_us_place_order"](
        symbol=args.symbol,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        trde_tp="00",
        dry_run=False,
        confirm=True,
    )
    _emit({"step": "place_confirmed", **placed})
    if not placed.get("success"):
        return 2

    order_id = extract_order_id(placed)
    if order_id is None:
        history = await tools["kiwoom_mock_us_get_order_history"](scope="open")
        _emit({"step": "reconcile_no_order_id", **history})
        _emit(
            {
                "step": "cleanup_required",
                "reason": "accepted order id was not a nine-digit value",
            }
        )
        return 2

    exit_code = 0
    try:
        history = await tools["kiwoom_mock_us_get_order_history"](
            scope="open", symbol=args.symbol
        )
        _emit({"step": "history_after_place", **history})
        if args.new_price is not None:
            modified = await tools["kiwoom_mock_us_modify_order"](
                order_id=order_id,
                symbol=args.symbol,
                new_price=args.new_price,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "modify_confirmed", **modified})
            if modified.get("success") and extract_order_id(modified):
                order_id = extract_order_id(modified) or order_id
    finally:
        cancelled = await tools["kiwoom_mock_us_cancel_order"](
            order_id=order_id,
            symbol=args.symbol,
            dry_run=False,
            confirm=True,
        )
        _emit({"step": "cancel_confirmed", **cancelled})
        if not cancelled.get("success"):
            exit_code = 2
            _emit(
                {
                    "step": "cleanup_required",
                    "order_id": order_id,
                    "reason": "cancel did not succeed",
                }
            )

    final_history = await tools["kiwoom_mock_us_get_order_history"](
        scope="open", symbol=args.symbol
    )
    _emit({"step": "final_open_orders", **final_history})
    positions = await tools["kiwoom_mock_us_get_positions"](symbol=args.symbol)
    _emit({"step": "final_positions", **positions})
    return exit_code


async def run_probe(args: argparse.Namespace) -> int:
    codes = parse_probe_codes(args.probe_order_types)
    if not codes:
        return 0
    if not args.confirm_probes:
        raise SmokeRejected(
            "--confirm-probes is required before advanced broker mutations"
        )
    exchange = str(await get_us_exchange_by_symbol(args.symbol)).strip().upper()
    try:
        stex_tp = constants.US_EXCHANGE_TO_STEX[exchange]
    except KeyError as exc:
        raise SmokeRejected(f"unsupported exchange={exchange!r}") from exc

    client = KiwoomMockUsClient.from_app_settings()
    orders = KiwoomUsOrderClient(client)
    exit_code = 0
    for code in codes:
        order_id: str | None = None
        accepted = False
        try:
            if args.probe_side == "buy":
                raw = await orders.place_buy_order(
                    symbol=args.symbol,
                    stex_tp=stex_tp,
                    quantity=args.quantity,
                    trde_tp=code,
                    price=args.price,
                )
            else:
                raw = await orders.place_sell_order(
                    symbol=args.symbol,
                    stex_tp=stex_tp,
                    quantity=args.quantity,
                    trde_tp=code,
                    price=args.price,
                    stop_price=args.stop_price,
                )
            accepted = int(raw.get("return_code", -1)) == 0
            order_id = extract_order_id(raw)
            _emit(
                {
                    "step": "probe_order_type",
                    "trde_tp": code,
                    "accepted": accepted,
                    "broker_response": raw,
                }
            )
            if accepted and order_id is None:
                exit_code = 2
        finally:
            if accepted and order_id is not None:
                cancelled = await orders.cancel_order(
                    original_order_no=order_id,
                    symbol=args.symbol,
                    stex_tp=stex_tp,
                )
                _emit(
                    {
                        "step": "probe_cancel",
                        "trde_tp": code,
                        "order_id": order_id,
                        "broker_response": cancelled,
                    }
                )
                if int(cancelled.get("return_code", -1)) != 0:
                    exit_code = 2
    return exit_code
~~~

- [ ] **Step 5: Implement parser and main dispatch**

~~~python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kiwoom US mock smoke (ROB-867)")
    parser.add_argument(
        "--mode", required=True, choices=["preflight", "preview", "full"]
    )
    parser.add_argument("--symbol")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--price", type=float)
    parser.add_argument("--new-price", type=float)
    parser.add_argument("--trde-tp", default="00")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--probe-order-types")
    parser.add_argument("--probe-side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--stop-price", type=float)
    parser.add_argument("--confirm-probes", action="store_true")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    if args.mode == "preflight":
        _emit(await run_preflight())
        if args.probe_order_types:
            return await run_probe(args)
        return 0
    if not args.symbol or not args.quantity:
        raise SmokeRejected("symbol and quantity are required")
    if args.trde_tp == "00" and args.price is None:
        raise SmokeRejected("limit order requires --price")
    if args.mode == "preview":
        _emit({"step": "preview", **await run_preview(args)})
        return 0
    return await run_full(args)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
~~~

- [ ] **Step 6: Run CLI tests and verify GREEN**

Run:

~~~bash
uv run --all-groups pytest tests/test_kiwoom_mock_us_smoke_cli.py -q
~~~

Expected: all tests pass; no live/mock network calls occur in unit tests.

- [ ] **Step 7: Commit**

~~~bash
git add scripts/kiwoom_mock_us_smoke.py tests/test_kiwoom_mock_us_smoke_cli.py
git commit -m "feat(ROB-867): add Kiwoom mock US smoke workflow"
~~~

---

### Task 8: Document the public contract and operator runbook

**Files:**
- Modify: app/mcp_server/README.md
- Create: docs/runbooks/kiwoom-mock-us-smoke.md

**Interfaces:**
- Consumes: final tool signatures and CLI flags from Tasks 5-7.
- Produces: user-facing MCP reference and evidence-first smoke procedure.

- [ ] **Step 1: Add the MCP README contract**

Add a Kiwoom US section containing this exact contract:

~~~markdown
### Kiwoom US mock tools (ROB-867)

The kiwoom_mock_us_* namespace is isolated from KR kiwoom_mock credentials and
always targets https://mockapi.kiwoom.com. DEFAULT registration requires
KIWOOM_MOCK_US_ENABLED=true; the KIWOOM profile registers the namespace but
calls still fail closed when US credentials are incomplete.

MCP order support is intentionally limited to:

| trde_tp | Meaning | price |
|---|---|---|
| 00 | limit | required and positive |
| 03 | market | must be omitted |

Other documented Kiwoom order types return error_code=unsupported_trde_tp
before network I/O. Their presence in Kiwoom documentation is not evidence that
the mock environment accepts them.

get_order_history(scope="open"|"today") uses ust21050 or ust21510.
get_positions uses ust21070. get_orderable_cash uses ust21160 and labels
d0_usd_fx_entr as cash_semantics=deposit_not_broker_orderable because the mock
environment rejects documented orderable-quantity TR ust31490 with RC9000.
~~~

- [ ] **Step 2: Create the runbook**

The runbook must contain these concrete commands:

~~~markdown
# Kiwoom Mock US Smoke

## Required environment

- KIWOOM_MOCK_US_ENABLED=true
- KIWOOM_MOCK_US_APP_KEY
- KIWOOM_MOCK_US_APP_SECRET
- KIWOOM_MOCK_US_ACCOUNT_NO
- KIWOOM_MOCK_BASE_URL=https://mockapi.kiwoom.com

The CLI prints missing key names only. It never prints values.

## Read-only preflight

uv run python -m scripts.kiwoom_mock_us_smoke --mode preflight

## Preview

uv run python -m scripts.kiwoom_mock_us_smoke \
  --mode preview --symbol NVDA --side buy --quantity 1 \
  --trde-tp 00 --price 1.00

## Confirmed distant-limit lifecycle

Choose an operator-reviewed non-marketable price. The CLI does not fetch or
invent a quote.

uv run python -m scripts.kiwoom_mock_us_smoke \
  --mode full --symbol NVDA --side buy --quantity 1 \
  --trde-tp 00 --price 1.00 --confirm

The sequence is preview, dry-run, confirmed submit, ust21050 open-order read,
cancel in finally, final open-order read, and position read. Exit 2 means manual
cleanup is required.

## Explicit advanced-type probe

The probe performs real mock broker mutations and is disabled unless both the
code list and second confirmation are present:

uv run python -m scripts.kiwoom_mock_us_smoke \
  --mode preflight --symbol NVDA --quantity 1 --price 1.00 \
  --probe-order-types 26,27,30 --probe-side buy --confirm-probes

Market-like or sell-only codes are not default probe inputs. Probe them only
with an operator-reviewed position and fill-risk plan. Every accepted order is
cancelled in finally; an unparsed order id or failed cancel exits 2.

## Known evidence

| TR/capability | Mock evidence |
|---|---|
| OAuth | return_code=0 on 2026-07-13 |
| ust21070 | return_code=0, empty new account |
| ust21050 | return_code=0, empty list |
| ust21110 | return_code=0 |
| ust21160 | return_code=0 |
| ust31490 | return_code=20, RC9000 unsupported |
| trde_tp 00 | not recorded by this implementation plan |
| trde_tp 03 | contract implemented; mutation smoke requires separate operator risk |
| BRK.B | unverified; DB dot form is passed unchanged |

Replace an evidence row only with an exact dated broker result. Do not infer
support from documentation.
~~~

- [ ] **Step 3: Verify documentation and focused tests**

Run:

~~~bash
git diff --check
uv run --all-groups pytest tests/test_mcp_kiwoom_us_order_variants.py tests/test_kiwoom_mock_us_smoke_cli.py -q
~~~

Expected: no whitespace errors and all focused tests pass.

- [ ] **Step 4: Commit**

~~~bash
git add app/mcp_server/README.md docs/runbooks/kiwoom-mock-us-smoke.md
git commit -m "docs(ROB-867): add Kiwoom mock US runbook"
~~~

---

### Task 9: Run complete verification and record evidence

**Files:**
- Modify only if verification exposes a defect; add a failing regression test before each correction.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: fresh test/lint/type evidence suitable for the Linear issue and PR.

- [ ] **Step 1: Run focused Kiwoom and governance tests**

~~~bash
uv run --all-groups pytest \
  tests/test_kiwoom_mock_us_config.py \
  tests/test_kiwoom_us_orders.py \
  tests/test_kiwoom_us_account.py \
  tests/test_mcp_kiwoom_shared.py \
  tests/test_mcp_kiwoom_us_order_variants.py \
  tests/test_kiwoom_mock_us_smoke_cli.py \
  tests/test_kiwoom_mock_config.py \
  tests/test_kiwoom_client_endpoint_guard.py \
  tests/test_kiwoom_auth_token_cache.py \
  tests/test_kiwoom_domestic_orders.py \
  tests/test_kiwoom_domestic_account.py \
  tests/test_mcp_kiwoom_order_variants.py \
  tests/test_mcp_profiles.py \
  tests/test_route_request_lanes.py \
  tests/test_route_request_registry_diff.py \
  tests/test_watch_triage_readonly_settings.py -q
~~~

Expected: zero failures.

- [ ] **Step 2: Run Ruff and ty**

~~~bash
uv run ruff check app/core/config.py app/services/brokers/kiwoom app/mcp_server/tooling/orders_kiwoom_shared.py app/mcp_server/tooling/orders_kiwoom_variants.py app/mcp_server/tooling/orders_kiwoom_us_variants.py scripts/kiwoom_mock_us_smoke.py tests/test_kiwoom_mock_us_config.py tests/test_kiwoom_us_orders.py tests/test_kiwoom_us_account.py tests/test_mcp_kiwoom_shared.py tests/test_mcp_kiwoom_us_order_variants.py tests/test_kiwoom_mock_us_smoke_cli.py
uv run ty check app/services/brokers/kiwoom app/mcp_server/tooling/orders_kiwoom_us_variants.py scripts/kiwoom_mock_us_smoke.py
~~~

Expected: both commands exit 0.

- [ ] **Step 3: Run the full non-live test suite**

~~~bash
make test
~~~

Expected: zero failures; live tests remain skipped unless --run-live is supplied.

- [ ] **Step 4: Run read-only operator preflight only when the operator env is available**

~~~bash
uv run python -m scripts.kiwoom_mock_us_smoke --mode preflight
~~~

Expected: missing_env_keys is empty and proven read-only TRs return explicit broker evidence. Do not run full or probe modes without a separate operator-approved symbol, price, session window, and cleanup owner.

- [ ] **Step 5: Review the diff against the design acceptance criteria**

Check:

~~~bash
git status --short
git diff --check HEAD~8..HEAD
git diff --stat HEAD~8..HEAD
~~~

Confirm:

1. no US-to-KR credential fallback
2. no live host selection
3. only 00/03 exposed by MCP
4. no ust31490 call
5. all mutations require dry_run/confirm
6. accepted smoke orders cancel in finally
7. registration, route, and readonly sets are exhaustive
8. docs make no unsupported capability claim

- [ ] **Step 6: Add a Linear completion comment after evidence exists**

Post a concise ROB-867 comment containing:

- commit/PR identifier
- focused and full test counts
- Ruff/ty results
- whether read-only preflight ran
- whether mutation smoke did not run, or the exact dated submit/cancel evidence
- any class-share or advanced-order-type capability still unverified

Do not move ROB-867 to Done until code is landed and every required automated
gate passes. Mutation smoke is optional operator evidence and must not be faked.
