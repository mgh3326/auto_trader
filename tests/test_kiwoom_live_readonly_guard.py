# tests/test_kiwoom_live_readonly_guard.py
"""Safety-boundary tests for the Kiwoom live read-only chart client.

Covers all 11 items of the Stage 1a safety design. Every test is offline: the
only transport used is ``httpx.MockTransport``, and the guard tests assert that
the transport is never reached at all.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.services.brokers.kiwoom.chart_compare as chart_compare_mod
import app.services.brokers.kiwoom.live_market_data as live_mod
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.live_market_data import (
    ALLOWED_API_IDS,
    ALLOWED_PATHS,
    CHART_PATH,
    KiwoomLiveReadOnlyApiIdError,
    KiwoomLiveReadOnlyClient,
    KiwoomLiveReadOnlyConfigurationError,
    KiwoomLiveReadOnlyDisabled,
    KiwoomLiveReadOnlyEndpointError,
    KiwoomLiveReadOnlyPathError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def armed(monkeypatch):
    """Arm the env gate (item 7) so dispatch-path tests can run."""

    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_live_marketdata_enabled", True)
    return cfg.settings


def _counting_transport() -> tuple[httpx.MockTransport, dict[str, Any]]:
    calls: dict[str, Any] = {"count": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        calls["requests"].append(request)
        return httpx.Response(
            200,
            json={"return_code": 0, "return_msg": "정상", "stk_dt_pole_chart_qry": []},
            headers={"cont-yn": "N", "next-key": ""},
        )

    return httpx.MockTransport(handler), calls


def _client(transport: httpx.MockTransport | None = None) -> KiwoomLiveReadOnlyClient:
    client = KiwoomLiveReadOnlyClient(app_key="ak", app_secret="sk")
    if transport is not None:
        client.set_transport_for_test(transport, token="TKN")
    return client


# ---------------------------------------------------------------------------
# Item 3 — api-id allowlist (regression test ①: order api-id injection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order_api_id",
    [
        constants.ORDER_BUY_API_ID,
        constants.ORDER_SELL_API_ID,
        constants.ORDER_MODIFY_API_ID,
        constants.ORDER_CANCEL_API_ID,
        constants.ACCOUNT_BALANCE_API_ID,
        constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
        constants.US_ORDER_BUY_API_ID,
    ],
)
async def test_order_and_account_api_ids_are_refused(armed, order_api_id):
    """① Injecting an order/account TR must raise before any HTTP dispatch."""

    transport, calls = _counting_transport()
    client = _client(transport)

    with pytest.raises(KiwoomLiveReadOnlyApiIdError):
        await client.post_chart(api_id=order_api_id, body={"stk_cd": "005930"})

    assert calls["count"] == 0


def test_allowlist_is_exactly_the_four_chart_trs():
    assert ALLOWED_API_IDS == frozenset({"ka10080", "ka10081", "ka10082", "ka10083"})
    assert ALLOWED_PATHS == frozenset({"/api/dostk/chart"})


@pytest.mark.asyncio
async def test_chart_api_id_is_accepted_and_sent(armed):
    transport, calls = _counting_transport()
    client = _client(transport)

    payload = await client.fetch_daily_chart(symbol="005930", base_dt="20260731")

    assert calls["count"] == 1
    request = calls["requests"][0]
    assert request.headers[constants.HEADER_API_ID] == constants.CHART_DAILY_API_ID
    assert request.headers[constants.HEADER_AUTHORIZATION] == "Bearer TKN"
    assert str(request.url) == "https://api.kiwoom.com/api/dostk/chart"
    assert payload["return_code"] == 0
    assert payload["continuation"] == {"cont_yn": "N", "next_key": ""}


# ---------------------------------------------------------------------------
# Item 4 — path allowlist (regression test ②)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_path",
    [
        "/api/dostk/ordr",
        "/api/dostk/acnt",
        "/api/us/ordr",
        "/api/dostk/chart/../ordr",
        "//api.kiwoom.com/api/dostk/chart",
        "https://api.kiwoom.com/api/dostk/chart",
        "api/dostk/chart",
        "",
        "/api/dostk/chart\nHost: evil.example.com",
    ],
)
async def test_non_chart_paths_are_refused(armed, bad_path):
    """② Any path other than the chart path must raise before dispatch."""

    transport, calls = _counting_transport()
    client = _client(transport)

    with pytest.raises(KiwoomLiveReadOnlyPathError):
        await client.post_chart(
            api_id=constants.CHART_DAILY_API_ID,
            body={"stk_cd": "005930"},
            path=bad_path,
        )

    assert calls["count"] == 0


# ---------------------------------------------------------------------------
# Item 6 — host pinned + re-validated (regression test ③)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_base_url",
    [
        "https://mockapi.kiwoom.com",
        "https://evil.example.com",
        "http://api.kiwoom.com",
        "https://api.kiwoom.com.evil.example.com",
    ],
)
def test_constructor_rejects_non_live_base_url(bad_base_url):
    """③ Only the exact live base URL is constructible — mock host included."""

    with pytest.raises(KiwoomLiveReadOnlyEndpointError):
        KiwoomLiveReadOnlyClient(app_key="ak", app_secret="sk", base_url=bad_base_url)


@pytest.mark.asyncio
async def test_post_chart_revalidates_resolved_host_before_dispatch(armed):
    """③ Post-construction tampering must still be caught right before send."""

    transport, calls = _counting_transport()
    client = _client(transport)
    client._base_url = "https://mockapi.kiwoom.com"  # type: ignore[attr-defined]

    with pytest.raises(KiwoomLiveReadOnlyEndpointError):
        await client.fetch_daily_chart(symbol="005930", base_dt="20260731")

    assert calls["count"] == 0


# ---------------------------------------------------------------------------
# Item 2 — no account surface (regression test ④)
# ---------------------------------------------------------------------------


def test_client_has_no_account_attribute():
    """④ Neither instance state nor the public API exposes an account."""

    client = _client()

    assert not hasattr(client, "account_no")
    assert not hasattr(client, "_account_no")

    offending_state = [name for name in vars(client) if "account" in name.lower()]
    assert offending_state == []

    offending_api = [
        name
        for name in dir(client)
        if ("account" in name.lower() or "acnt" in name.lower())
    ]
    assert offending_api == []


def test_client_constructor_rejects_an_account_number_kwarg():
    with pytest.raises(TypeError):
        KiwoomLiveReadOnlyClient(  # type: ignore[call-arg]
            app_key="ak", app_secret="sk", account_no="12345678-01"
        )


# ---------------------------------------------------------------------------
# Item 7 — env gate, default false
# ---------------------------------------------------------------------------


def test_env_gate_defaults_to_false():
    from app.core.config import Settings

    assert Settings.model_fields["kiwoom_live_marketdata_enabled"].default is False


@pytest.mark.asyncio
async def test_dispatch_fails_closed_when_gate_disabled(monkeypatch):
    """Even a directly-constructed client cannot send while the gate is off."""

    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_live_marketdata_enabled", False)
    transport, calls = _counting_transport()
    client = _client(transport)

    with pytest.raises(KiwoomLiveReadOnlyDisabled):
        await client.fetch_daily_chart(symbol="005930", base_dt="20260731")

    assert calls["count"] == 0


def test_from_app_settings_fails_closed_when_disabled(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_live_marketdata_enabled", False)
    with pytest.raises(KiwoomLiveReadOnlyConfigurationError) as exc:
        KiwoomLiveReadOnlyClient.from_app_settings()
    assert "KIWOOM_LIVE_MARKETDATA_ENABLED" in str(exc.value)


def test_from_app_settings_fails_closed_when_credentials_missing(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_live_marketdata_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_live_app_key", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_live_app_secret", None)
    with pytest.raises(KiwoomLiveReadOnlyConfigurationError) as exc:
        KiwoomLiveReadOnlyClient.from_app_settings()
    message = str(exc.value)
    assert "KIWOOM_LIVE_APP_KEY" in message
    assert "KIWOOM_LIVE_APP_SECRET" in message


# ---------------------------------------------------------------------------
# Item 9 — minimal Settings surface, no account number
# ---------------------------------------------------------------------------


def test_settings_expose_only_key_secret_and_base_url_plus_the_gate():
    from app.core.config import Settings

    live_fields = {
        name for name in Settings.model_fields if name.startswith("kiwoom_live_")
    }
    assert live_fields == {
        "kiwoom_live_marketdata_enabled",  # item 7 gate
        "kiwoom_live_app_key",
        "kiwoom_live_app_secret",
        "kiwoom_live_base_url",
    }


def test_settings_do_not_define_a_live_account_number():
    from app.core.config import Settings

    assert "kiwoom_live_account_no" not in Settings.model_fields
    assert "kiwoom_account_no" not in Settings.model_fields


def test_live_validator_never_requires_an_account_number(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_live_marketdata_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_live_app_key", "ak")
    monkeypatch.setattr(cfg.settings, "kiwoom_live_app_secret", "sk")
    assert cfg.validate_kiwoom_live_marketdata_config(cfg.settings) == []


# ---------------------------------------------------------------------------
# Items 5 + 10 — AST guard
# ---------------------------------------------------------------------------

_GUARDED_MODULES = (live_mod, chart_compare_mod)

#: Names the live modules must never reference (items 5 and 10).
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "ORDER_BUY_API_ID",
        "ORDER_SELL_API_ID",
        "ORDER_MODIFY_API_ID",
        "ORDER_CANCEL_API_ID",
        "ORDER_PATH",
        "US_ORDER_BUY_API_ID",
        "US_ORDER_SELL_API_ID",
        "US_ORDER_MODIFY_API_ID",
        "US_ORDER_CANCEL_API_ID",
        "US_ORDER_PATH",
        "MOCK_REJECTED_EXCHANGES",
        # Item 10 — the account number must not be named at all.
        "kiwoom_account_no",
        "KIWOOM_ACCOUNT_NO",
        "account_no",
    }
)

#: Import targets the live modules must not reach.
_FORBIDDEN_IMPORT_FRAGMENTS: tuple[str, ...] = (
    "domestic_orders",
    "us_orders",
    "order_preflight",
    "domestic_account",
    "us_account",
    "us_client",
    "kiwoom.client",
    "trading_service",
    "ledger",
    "reconcil",
)

#: Strings that must not appear as literals (a stringly-typed bypass).
_FORBIDDEN_STRING_FRAGMENTS: tuple[str, ...] = (
    "KIWOOM_ACCOUNT_NO",
    "kiwoom_account_no",
    "account_no",
    "/api/dostk/ordr",
    "/api/us/ordr",
    "kt10000",
    "kt10001",
    "kt10002",
    "kt10003",
)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Ids of Constant nodes that are docstrings (prose, not code references)."""

    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _scan_source(source: str, filename: str) -> list[str]:
    """Return guard violations for one module's source."""

    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_ids(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fragment in _FORBIDDEN_IMPORT_FRAGMENTS:
                    if fragment in alias.name:
                        violations.append(
                            f"{filename}: imports {alias.name!r} ({fragment!r})"
                        )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for fragment in _FORBIDDEN_IMPORT_FRAGMENTS:
                if fragment in module:
                    violations.append(
                        f"{filename}: imports from {module!r} ({fragment!r})"
                    )
            for alias in node.names:
                if alias.name in _FORBIDDEN_NAMES:
                    violations.append(
                        f"{filename}: imports forbidden name {alias.name!r}"
                    )
            # Binding the constants module wholesale would grant attribute
            # access to every order TR, defeating the name checks below.
            if module.endswith("brokers.kiwoom") and any(
                alias.name == "constants" for alias in node.names
            ):
                violations.append(
                    f"{filename}: binds the whole constants module; "
                    "use named imports so the order TRs stay unreachable"
                )
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            violations.append(f"{filename}:{node.lineno}: references {node.id!r}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.append(f"{filename}:{node.lineno}: attribute {node.attr!r}")
        elif isinstance(node, ast.keyword) and node.arg in _FORBIDDEN_NAMES:
            violations.append(f"{filename}:{node.lineno}: keyword arg {node.arg!r}")
        elif isinstance(node, ast.arg) and node.arg in _FORBIDDEN_NAMES:
            violations.append(f"{filename}:{node.lineno}: parameter {node.arg!r}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            for fragment in _FORBIDDEN_STRING_FRAGMENTS:
                if fragment in node.value:
                    violations.append(
                        f"{filename}:{node.lineno}: string literal contains "
                        f"{fragment!r}"
                    )

    return violations


def test_live_modules_reference_no_order_or_account_surface():
    """Items 5 + 10 — scoped to the NEW live modules only, not retroactive."""

    violations: list[str] = []
    for module in _GUARDED_MODULES:
        path = Path(module.__file__)
        violations.extend(_scan_source(path.read_text(encoding="utf-8"), path.name))

    assert not violations, "live read-only guard violated:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "order constant import",
            "from app.services.brokers.kiwoom.constants import ORDER_BUY_API_ID\n"
            "X = ORDER_BUY_API_ID\n",
        ),
        (
            "constants module attribute access",
            "from app.services.brokers.kiwoom import constants\n"
            "X = constants.ORDER_SELL_API_ID\n",
        ),
        (
            "order module import",
            "from app.services.brokers.kiwoom.domestic_orders import "
            "KiwoomDomesticOrderClient\n",
        ),
        (
            "settings account read",
            "def f(settings):\n    return settings.kiwoom_account_no\n",
        ),
        (
            "stringly-typed account read",
            'def f(settings):\n    return getattr(settings, "kiwoom_account_no")\n',
        ),
        (
            "account_no constructor parameter",
            "class C:\n    def __init__(self, *, account_no):\n"
            "        self._x = account_no\n",
        ),
        (
            "hardcoded order path",
            'PATH = "/api/dostk/ordr"\n',
        ),
        (
            "hardcoded order TR code",
            'API = "kt10000"\n',
        ),
    ],
)
def test_ast_guard_actually_catches_violations(label, source):
    """The guard must have teeth — each synthetic breach is detected."""

    assert _scan_source(source, f"<{label}>"), f"guard missed: {label}"


def test_ast_guard_does_not_fire_on_clean_source():
    clean = (
        '"""Docstring may mention KIWOOM_ACCOUNT_NO as prose."""\n'
        "from app.services.brokers.kiwoom.constants import CHART_DAILY_API_ID\n"
        "X = CHART_DAILY_API_ID\n"
    )
    assert _scan_source(clean, "<clean>") == []


# ---------------------------------------------------------------------------
# Items 1 + 8⑤ — no regression on the mock client / auth / order path
# ---------------------------------------------------------------------------


def test_mock_client_still_refuses_the_live_host():
    """⑤ The mock boundary is untouched by this change."""

    from app.services.brokers.kiwoom.client import (
        KiwoomEndpointError,
        KiwoomMockClient,
    )

    with pytest.raises(KiwoomEndpointError):
        KiwoomMockClient(
            base_url=constants.LIVE_BASE_URL,
            app_key="ak",
            app_secret="sk",
            account_no="123",
        )


def test_mock_auth_client_still_refuses_the_live_host():
    """⑤ ``auth.py``'s mock-only assertion is unmodified and still fires."""

    from app.services.brokers.kiwoom.auth import KiwoomAuthClient

    with pytest.raises(ValueError, match="mock-only"):
        KiwoomAuthClient(
            base_url=constants.LIVE_BASE_URL, app_key="ak", app_secret="sk"
        )


def test_order_constants_and_exchange_guard_are_unchanged():
    """⑤ Order TRs and the KRX-only order boundary keep their values."""

    assert constants.ORDER_BUY_API_ID == "kt10000"
    assert constants.ORDER_SELL_API_ID == "kt10001"
    assert constants.ORDER_MODIFY_API_ID == "kt10002"
    assert constants.ORDER_CANCEL_API_ID == "kt10003"
    assert constants.ORDER_PATH == "/api/dostk/ordr"
    assert constants.MOCK_REJECTED_EXCHANGES == frozenset({"NXT", "SOR"})
    assert constants.MOCK_BASE_URL == "https://mockapi.kiwoom.com"


def test_live_module_does_not_subclass_the_mock_client():
    """Item 1 — separation, not extension."""

    from app.services.brokers.kiwoom.auth import KiwoomAuthClient
    from app.services.brokers.kiwoom.client import KiwoomMockClient

    assert not issubclass(KiwoomLiveReadOnlyClient, KiwoomMockClient)
    assert not issubclass(live_mod.KiwoomLiveReadOnlyAuthClient, KiwoomAuthClient)


def test_live_token_cache_namespace_is_disjoint_from_mock():
    """A live token must never be served to a mock caller, or vice versa."""

    from app.services.brokers.kiwoom.auth import KiwoomAuthClient

    live_auth = live_mod.KiwoomLiveReadOnlyAuthClient(app_key="ak", app_secret="sk")
    mock_auth = KiwoomAuthClient(
        base_url=constants.MOCK_BASE_URL, app_key="ak", app_secret="sk"
    )

    assert live_auth.token_key != mock_auth.token_key
    assert live_auth.lock_key != mock_auth.lock_key
    assert "live-ro" in live_auth.token_key


# ---------------------------------------------------------------------------
# Request-shape conformance with the official chart docs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_request_body_matches_official_contract(armed):
    import json

    transport, calls = _counting_transport()
    client = _client(transport)

    await client.fetch_daily_chart(symbol="005930", base_dt="20260731")

    body = json.loads(calls["requests"][0].read())
    assert body == {"stk_cd": "005930", "base_dt": "20260731", "upd_stkpc_tp": "1"}


@pytest.mark.asyncio
async def test_minute_request_omits_optional_base_dt(armed):
    import json

    transport, calls = _counting_transport()
    client = _client(transport)

    await client.fetch_minute_chart(symbol="005930", tic_scope="5")

    body = json.loads(calls["requests"][0].read())
    assert body == {"stk_cd": "005930", "tic_scope": "5", "upd_stkpc_tp": "1"}
    assert calls["requests"][0].headers[constants.HEADER_API_ID] == "ka10080"


@pytest.mark.asyncio
async def test_minute_rejects_undocumented_tic_scope(armed):
    transport, calls = _counting_transport()
    client = _client(transport)

    with pytest.raises(ValueError):
        await client.fetch_minute_chart(symbol="005930", tic_scope="7")

    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_all_four_chart_trs_route_to_the_chart_path(armed):
    transport, calls = _counting_transport()
    client = _client(transport)

    await client.fetch_daily_chart(symbol="005930", base_dt="20260731")
    await client.fetch_minute_chart(symbol="005930", tic_scope="5")
    await client.fetch_weekly_chart(symbol="005930", base_dt="20260731")
    await client.fetch_monthly_chart(symbol="005930", base_dt="20260731")

    assert calls["count"] == 4
    assert {r.headers[constants.HEADER_API_ID] for r in calls["requests"]} == set(
        ALLOWED_API_IDS
    )
    for request in calls["requests"]:
        assert request.url.path == CHART_PATH
        assert request.url.host == "api.kiwoom.com"
