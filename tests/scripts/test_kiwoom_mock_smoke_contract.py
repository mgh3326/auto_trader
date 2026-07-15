"""ROB-898 — Kiwoom mock account-read contract sweep tests (review-hardened).

Verifies ``--mode contract`` in ``scripts/kiwoom_mock_smoke.py``:

* Four read endpoints called (kt00018, kt00001, kt00010, kt00009).
* Strict pass: success==True AND return_code==0 AND exact mock provenance AND
  expected api_id. Inconsistent envelopes (success=true + nonzero RC) fail.
* Zero broker mutations. Mutation tools are guarded.
* Secret/account/token values never appear in stdout — redaction uses numeric
  pattern fail-closed, not English keyword denylist.
* Live/production host detection via urlparse + exact hostname.
* Malformed provenance/payload (non-dict) fails and continues.
* Single request-scoped KiwoomMockClient reused across sweep.
* Injectable pacing — tests don't actually sleep.
* contract_fields output per step.
* MockTransport assertions prove actual request body.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.mcp_server.tooling import orders_kiwoom_variants as kvar
from app.services.brokers.kiwoom import constants as kw_constants
from app.services.brokers.kiwoom.client import KiwoomMockClient
from scripts import kiwoom_mock_smoke as smoke

SENTINEL_SECRET = "KIWOOM_MOCK_SECRET_MUST_NOT_LEAK_ABCDEF"
SENTINEL_TOKEN = "BEARER_TOKEN_MUST_NOT_LEAK_XYZ"
SENTINEL_ACCT = "88012345678"
SENTINEL_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJhY2N0IjoiMTIzNC01Ni03ODkwIn0.signature"
SENTINEL_HYPHENATED_ACCT = "1234-56-7890"
SENTINEL_KR_ACCOUNT = f"계좌번호 {SENTINEL_HYPHENATED_ACCT}"


def _sensitive_samples() -> tuple[str, ...]:
    return (
        SENTINEL_SECRET,
        SENTINEL_TOKEN,
        SENTINEL_ACCT,
        SENTINEL_JWT,
        SENTINEL_HYPHENATED_ACCT,
        SENTINEL_KR_ACCOUNT,
        "Authorization: Bearer",
        "access_token=",
    )


def _assert_sensitive_values_absent(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    for sample in _sensitive_samples():
        assert sample not in rendered


def _parse_stdout(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    raw = capsys.readouterr().out.strip()
    return [json.loads(line) for line in raw.splitlines() if line]


def _summary(lines: list[dict[str, Any]]) -> dict[str, Any]:
    return [ln for ln in lines if ln["step"] == "contract_sweep_summary"][0]


def _steps(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ln for ln in lines if ln["step"] == "contract_step"]


def _mock_handler(
    *,
    return_codes: dict[str, int] | None = None,
    return_msg_override: str | None = None,
    captured_bodies: dict[str, dict[str, Any]] | None = None,
) -> Any:
    rc_map = return_codes or {}
    msg = return_msg_override

    def handler(request: httpx.Request) -> httpx.Response:
        api_id = request.headers.get("api-id", "")
        try:
            body = json.loads(request.content.decode()) if request.content else {}
        except Exception:
            body = {}
        if captured_bodies is not None:
            captured_bodies[api_id] = body
        rc = rc_map.get(api_id, 0)
        base: dict[str, Any] = {
            "return_code": rc,
            "return_msg": msg or ("\uc815\uc0c1" if rc == 0 else "\uc624\ub958"),
        }
        if api_id == kw_constants.ACCOUNT_BALANCE_API_ID:
            base["acnt_evlt_remn_indv_tot"] = []
        elif api_id == kw_constants.ACCOUNT_DEPOSIT_API_ID:
            base["ord_alow_amt"] = "5000000"
        elif api_id == kw_constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID:
            base["ord_alowa"] = "5000000"
        elif api_id == kw_constants.ACCOUNT_ORDER_STATUS_API_ID:
            base["acnt_ord_cntr_prst_array"] = []
        return httpx.Response(200, json=base)

    return handler


def _make_mock_client(handler: Any) -> KiwoomMockClient:
    client = KiwoomMockClient(
        base_url=kw_constants.MOCK_BASE_URL,
        app_key="test-app-key-abcdef",
        app_secret="test-app-secret-ghijkl",
        account_no=SENTINEL_ACCT,
    )
    transport = httpx.MockTransport(handler)
    client.set_transport_for_test(transport, token="test-bearer-token-xyz")
    return client


@pytest.fixture
def contract_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(kvar, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.MOCK_BASE_URL
    )
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_app_key", "test-app-key-abcdef")
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_app_secret", "test-app-secret-ghijkl"
    )
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_account_no", SENTINEL_ACCT)


def _setup_broker(
    monkeypatch: pytest.MonkeyPatch,
    contract_env: None,
    handler: Any,
) -> KiwoomMockClient:
    client = _make_mock_client(handler)
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )
    return client


def _contract_args() -> Any:
    return smoke.build_parser().parse_args(["--mode", "contract"])


async def _noop_sleep(_seconds: float) -> None:
    pass


# ---------------------------------------------------------------------------
# 1. Four endpoints + contract_fields + request body proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_endpoints_with_contract_fields(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, dict[str, Any]] = {}
    _setup_broker(monkeypatch, contract_env, _mock_handler(captured_bodies=captured))

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    steps = _steps(lines)
    assert len(steps) == 4

    api_ids = {ln["expected_api_id"] for ln in steps}
    assert api_ids == {"kt00018", "kt00001", "kt00010", "kt00009"}

    for step in steps:
        assert "contract_fields" in step
        cf = step["contract_fields"]
        assert "request_body" in cf

    assert rc == 0


@pytest.mark.asyncio
async def test_request_body_captured_via_mocktransport(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, dict[str, Any]] = {}
    _setup_broker(monkeypatch, contract_env, _mock_handler(captured_bodies=captured))

    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)

    assert kw_constants.ACCOUNT_BALANCE_API_ID in captured
    assert kw_constants.ACCOUNT_DEPOSIT_API_ID in captured
    assert kw_constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID in captured
    assert kw_constants.ACCOUNT_ORDER_STATUS_API_ID in captured

    assert captured[kw_constants.ACCOUNT_BALANCE_API_ID] == {
        "qry_tp": kw_constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT,
        "dmst_stex_tp": kw_constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
    }
    assert captured[kw_constants.ACCOUNT_DEPOSIT_API_ID] == {
        "qry_tp": kw_constants.ACCOUNT_DEPOSIT_QRY_TP_DEFAULT,
    }
    assert captured[kw_constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID] == {
        "stk_cd": "005930",
        "trde_tp": kw_constants.TRADE_TYPE_BUY,
        "uv": "50000",
    }
    assert captured[kw_constants.ACCOUNT_ORDER_STATUS_API_ID] == {
        "stk_bond_tp": kw_constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT,
    }


# ---------------------------------------------------------------------------
# 2. Strict pass: success + RC==0 + provenance + api_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inconsistent_envelope_success_true_nonzero_rc_fails(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler(
        return_codes={"kt00018": 2, "kt00001": 2, "kt00010": 2, "kt00009": 2}
    )
    _setup_broker(monkeypatch, contract_env, handler)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    steps = _steps(lines)

    for step in steps:
        assert step["pass"] is False
        assert step["return_code"] == 2
        assert step["return_code_is_zero"] is False

    assert _summary(lines)["overall_pass"] is False
    assert rc == 2


@pytest.mark.asyncio
async def test_rc20_treated_as_failure(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler(
        return_codes={kw_constants.ACCOUNT_DEPOSIT_API_ID: 20},
    )
    _setup_broker(monkeypatch, contract_env, handler)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    deposit = [s for s in _steps(lines) if s["stage"] == "deposit"][0]

    assert deposit["success"] is False
    assert deposit["pass"] is False
    assert deposit["return_code"] == 20
    assert deposit["return_code_is_zero"] is False
    assert rc == 2


@pytest.mark.asyncio
async def test_any_nonzero_rc_fails(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler(return_codes={"kt00009": 99})
    _setup_broker(monkeypatch, contract_env, handler)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    hist = [s for s in _steps(lines) if s["stage"] == "order_history"][0]

    assert hist["pass"] is False
    assert hist["return_code"] == 99
    assert hist["return_code_is_zero"] is False
    assert rc == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_success", ["false", "true", 1, [], object()])
async def test_success_must_be_exact_bool_true(
    bad_success: Any,
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler()
    client = _make_mock_client(handler)
    monkeypatch.setattr(
        KiwoomMockClient, "from_app_settings", classmethod(lambda cls: client)
    )

    original_tools = smoke._tools

    def _patched_tools() -> dict[str, Any]:
        tools = original_tools()

        async def bad_pos(**_kw: Any) -> dict[str, Any]:
            return {
                "success": bad_success,
                "provenance": {
                    "broker": "kiwoom",
                    "environment": "mock",
                    "account_mode": "kiwoom_mock",
                    "host": "mockapi.kiwoom.com",
                    "api_id": kw_constants.ACCOUNT_BALANCE_API_ID,
                },
                "broker_response": {
                    "return_code": 0,
                    "return_msg": "정상",
                },
            }

        tools["kiwoom_mock_get_positions"] = bad_pos
        return tools

    monkeypatch.setattr(smoke, "_tools", _patched_tools)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    pos_step = [s for s in _steps(lines) if s["stage"] == "positions"][0]

    assert pos_step["success"] is False
    assert pos_step["pass"] is False
    assert "success_false" in pos_step["fail_reasons"]
    assert rc == 2


# ---------------------------------------------------------------------------
# 3. Exact mock provenance — live provenance never passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_provenance_rejected(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler()
    client = _make_mock_client(handler)
    monkeypatch.setattr(
        KiwoomMockClient, "from_app_settings", classmethod(lambda cls: client)
    )

    original_tools = smoke._tools

    def _make_live_wrapper(orig: Any) -> Any:
        async def wrapped(**kw: Any) -> dict[str, Any]:
            result = await orig(**kw)
            if isinstance(result, dict) and isinstance(result.get("provenance"), dict):
                result["provenance"]["environment"] = "live"
                result["provenance"]["host"] = "api.kiwoom.com"
            return result

        return wrapped

    def _patched_tools() -> dict[str, Any]:
        tools = original_tools()
        for name in tools:
            tools[name] = _make_live_wrapper(tools[name])
        return tools

    monkeypatch.setattr(smoke, "_tools", _patched_tools)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    steps = _steps(lines)

    for step in steps:
        assert step["provenance_mock"] is False
        assert step["pass"] is False

    assert _summary(lines)["overall_pass"] is False
    assert rc == 2


# ---------------------------------------------------------------------------
# 4. Exact host verification (canonical URL only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "reason"),
    [
        ("http://mockapi.kiwoom.com", "mock_base_url_scheme_invalid"),
        ("https://mockapi.kiwoom.com:443", "mock_base_url_port_disallowed"),
        ("https://mockapi.kiwoom.com/", "mock_base_url_path_disallowed"),
        ("https://mockapi.kiwoom.com/path", "mock_base_url_path_disallowed"),
        (
            f"https://mockapi.kiwoom.com?access_token={SENTINEL_TOKEN}",
            "mock_base_url_query_disallowed",
        ),
        (
            f"https://mockapi.kiwoom.com#{SENTINEL_SECRET}",
            "mock_base_url_fragment_disallowed",
        ),
        (
            f"https://user:{SENTINEL_SECRET}@mockapi.kiwoom.com",
            "mock_base_url_userinfo_disallowed",
        ),
        (kw_constants.LIVE_BASE_URL, "mock_base_url_live_host_disallowed"),
        ("HTTPS://mockapi.kiwoom.com", "mock_base_url_noncanonical"),
        ("https://MOCKAPI.KIWOOM.COM", "mock_base_url_noncanonical"),
        (" https://mockapi.kiwoom.com", "mock_base_url_whitespace_disallowed"),
        ("https://mockapi.kiwoom.com ", "mock_base_url_whitespace_disallowed"),
    ],
)
def test_host_rejects_noncanonical_variants(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    reason: str,
) -> None:
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_base_url", base_url)
    assert smoke._verify_mock_host() == reason


def test_host_accepts_exact_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.MOCK_BASE_URL
    )
    assert smoke._verify_mock_host() is None


@pytest.mark.asyncio
async def test_live_host_blocked_exit2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smoke, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(kvar, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.LIVE_BASE_URL
    )
    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    assert rc == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        f"https://mockapi.kiwoom.com?account={SENTINEL_HYPHENATED_ACCT}",
        f"https://user:{SENTINEL_SECRET}@mockapi.kiwoom.com",
    ],
)
async def test_noncanonical_host_fails_before_client_creation_without_leaking_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    base_url: str,
) -> None:
    monkeypatch.setattr(smoke, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(kvar, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_base_url", base_url)

    called = False

    def _boom(_cls: type[KiwoomMockClient]) -> KiwoomMockClient:
        nonlocal called
        called = True
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(KiwoomMockClient, "from_app_settings", classmethod(_boom))

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)

    assert rc == 2
    assert called is False
    assert lines == [
        {
            "step": "contract_preflight",
            "ok": False,
            "error": "mock_host_verification_failed",
            "reason": lines[0]["reason"],
            "kst_time": lines[0]["kst_time"],
        }
    ]
    _assert_sensitive_values_absent(lines)


# ---------------------------------------------------------------------------
# 5. Malformed response — fail and continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_provenance_fails_and_continues(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler()
    client = _make_mock_client(handler)
    monkeypatch.setattr(
        KiwoomMockClient, "from_app_settings", classmethod(lambda cls: client)
    )

    original_tools = smoke._tools

    def _patched_tools() -> dict[str, Any]:
        tools = original_tools()

        async def broken_pos(**_kw: Any) -> dict[str, Any]:
            return {"success": True, "provenance": "not-a-dict"}

        tools["kiwoom_mock_get_positions"] = broken_pos
        return tools

    monkeypatch.setattr(smoke, "_tools", _patched_tools)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    steps = _steps(lines)

    pos_step = [s for s in steps if s["stage"] == "positions"][0]
    assert pos_step["pass"] is False
    assert pos_step["provenance_mock"] is False

    assert len([s for s in steps if s["stage"] != "positions"]) == 3
    assert rc == 2


@pytest.mark.asyncio
async def test_non_dict_payload_fails(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler()
    client = _make_mock_client(handler)
    monkeypatch.setattr(
        KiwoomMockClient, "from_app_settings", classmethod(lambda cls: client)
    )

    original_tools = smoke._tools

    def _patched_tools() -> dict[str, Any]:
        tools = original_tools()

        async def bad_pos(**_kw: Any) -> str:
            return "not-a-dict"

        tools["kiwoom_mock_get_positions"] = bad_pos
        return tools

    monkeypatch.setattr(smoke, "_tools", _patched_tools)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    pos_step = [s for s in _steps(lines) if s["stage"] == "positions"][0]

    assert pos_step["pass"] is False
    assert pos_step["fail_reason"] == "malformed_response"
    assert rc == 2


# ---------------------------------------------------------------------------
# 6. Redaction — numeric patterns + configured values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_sensitive_values_redacted(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler(
        return_msg_override=f"error key=test-app-key-abcdef acct={SENTINEL_ACCT}",
    )
    _setup_broker(monkeypatch, contract_env, handler)

    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    output = capsys.readouterr().out

    assert "test-app-key-abcdef" not in output
    assert "test-app-secret-ghijkl" not in output
    assert SENTINEL_ACCT not in output
    assert "test-bearer-token-xyz" not in output


@pytest.mark.asyncio
async def test_token_like_values_redacted_from_return_msg_and_error_detail(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler(
        return_msg_override=(
            f"Authorization: Bearer {SENTINEL_TOKEN} access_token={SENTINEL_TOKEN}"
        ),
    )
    client = _make_mock_client(handler)
    monkeypatch.setattr(
        KiwoomMockClient, "from_app_settings", classmethod(lambda cls: client)
    )

    original_tools = smoke._tools

    def _patched_tools() -> dict[str, Any]:
        tools = original_tools()

        async def leaking_history(**_kw: Any) -> dict[str, Any]:
            return {
                "success": False,
                "error": "kiwoom_mock_transport_error",
                "error_detail": (
                    f"Bearer {SENTINEL_TOKEN} authorization={SENTINEL_TOKEN}"
                ),
                "provenance": {
                    "broker": "kiwoom",
                    "environment": "mock",
                    "account_mode": "kiwoom_mock",
                    "host": "mockapi.kiwoom.com",
                    "api_id": kw_constants.ACCOUNT_ORDER_STATUS_API_ID,
                },
                "broker_response": {
                    "return_code": 2,
                    "return_msg": f"token={SENTINEL_TOKEN}",
                },
                "orders": [],
            }

        tools["kiwoom_mock_get_order_history"] = leaking_history
        return tools

    monkeypatch.setattr(smoke, "_tools", _patched_tools)

    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    output = json.dumps(lines, ensure_ascii=False)
    history = [s for s in _steps(lines) if s["stage"] == "order_history"][0]

    assert SENTINEL_TOKEN not in output
    assert "Authorization: Bearer" not in output
    assert history["return_msg"] == "[SANITIZED]"
    assert history["error_detail"] == "[SANITIZED]"


@pytest.mark.asyncio
async def test_run_contract_step_redacts_untrusted_success_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def safe_tool(**_kw: Any) -> dict[str, Any]:
        return {
            "success": True,
            "provenance": {
                "broker": "kiwoom",
                "environment": "mock",
                "account_mode": "kiwoom_mock",
                "host": "mockapi.kiwoom.com",
                "api_id": kw_constants.ACCOUNT_BALANCE_API_ID,
            },
            "broker_response": {
                "return_code": 0,
                "return_msg": f"Authorization: Bearer {SENTINEL_JWT}",
            },
        }

    step = await smoke._run_contract_step(
        tools={"kiwoom_mock_get_positions": safe_tool},
        tool_name="kiwoom_mock_get_positions",
        expected_api_id=kw_constants.ACCOUNT_BALANCE_API_ID,
        evidence_kind="positions",
        deploy_sha="deadbee",
        contract_fields={"request_body": {"qry_tp": "1"}},
    )
    lines = _parse_stdout(capsys)

    assert step["pass"] is True
    assert step["return_msg"] == "[SANITIZED]"
    _assert_sensitive_values_absent(step)
    _assert_sensitive_values_absent(lines)


@pytest.mark.asyncio
async def test_run_contract_step_redacts_untrusted_failure_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def hostile_tool(**_kw: Any) -> dict[str, Any]:
        return {
            "success": True,
            "error": f"error={SENTINEL_SECRET}",
            "error_detail": f"{SENTINEL_KR_ACCOUNT} access_token={SENTINEL_TOKEN}",
            "provenance": {
                "broker": "kiwoom",
                "environment": "mock",
                "account_mode": "kiwoom_mock",
                "host": "mockapi.kiwoom.com",
                "api_id": f"api_id={SENTINEL_TOKEN}",
            },
            "broker_response": {
                "return_code": f"return_code={SENTINEL_JWT}",
                "return_msg": f"acct={SENTINEL_HYPHENATED_ACCT}",
            },
        }

    step = await smoke._run_contract_step(
        tools={"kiwoom_mock_get_positions": hostile_tool},
        tool_name="kiwoom_mock_get_positions",
        expected_api_id=kw_constants.ACCOUNT_BALANCE_API_ID,
        evidence_kind="positions",
        deploy_sha="deadbee",
        contract_fields={"request_body": {"qry_tp": "1"}},
    )
    lines = _parse_stdout(capsys)

    assert step["pass"] is False
    assert step["actual_api_id"] == "[SANITIZED]"
    assert step["return_code"] == "[SANITIZED]"
    assert step["return_msg"] == "[SANITIZED]"
    assert step["error_code"] == "[SANITIZED]"
    assert step["error_detail"] == "[SANITIZED]"
    assert step["fail_reasons"] == ["return_code_nonzero", "api_id_mismatch"]
    _assert_sensitive_values_absent(step)
    _assert_sensitive_values_absent(lines)


@pytest.mark.asyncio
async def test_run_contract_step_redacts_exception_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exploding_tool(**_kw: Any) -> dict[str, Any]:
        raise RuntimeError(
            f"Authorization: Bearer {SENTINEL_TOKEN} {SENTINEL_KR_ACCOUNT}"
        )

    step = await smoke._run_contract_step(
        tools={"kiwoom_mock_get_positions": exploding_tool},
        tool_name="kiwoom_mock_get_positions",
        expected_api_id=kw_constants.ACCOUNT_BALANCE_API_ID,
        evidence_kind="positions",
        deploy_sha="deadbee",
        contract_fields={"request_body": {"qry_tp": "1"}},
    )
    lines = _parse_stdout(capsys)

    assert step["pass"] is False
    assert step["fail_reason"] == "exception"
    assert step["error_type"] == "RuntimeError"
    assert step["error_detail"] == "[SANITIZED]"
    _assert_sensitive_values_absent(step)
    _assert_sensitive_values_absent(lines)


def test_sanitize_redacts_digit_runs() -> None:
    assert smoke._sanitize_return_msg("ok") == "ok"
    assert smoke._sanitize_return_msg("error 12345678") == "[SANITIZED]"
    assert smoke._sanitize_return_msg(None) == ""


@pytest.mark.parametrize(
    "text",
    [
        SENTINEL_KR_ACCOUNT,
        SENTINEL_HYPHENATED_ACCT,
        f"jwt={SENTINEL_JWT}",
        f"token={SENTINEL_TOKEN}",
    ],
)
def test_sanitize_redacts_account_and_token_like_values(text: str) -> None:
    assert smoke._sanitize_return_msg(text) == "[SANITIZED]"


def test_sanitize_redacts_configured_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_app_key", "MY_SECRET_KEY_123")
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_app_secret", "")
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_account_no", "12345678")
    assert smoke._sanitize_return_msg("data MY_SECRET_KEY_123 here") == "[SANITIZED]"


def test_is_return_code_zero_strict() -> None:
    assert smoke._is_return_code_zero(0) is True
    assert smoke._is_return_code_zero("0") is True
    assert smoke._is_return_code_zero(1) is False
    assert smoke._is_return_code_zero("2") is False
    assert smoke._is_return_code_zero(None) is False
    assert smoke._is_return_code_zero(True) is False
    assert smoke._is_return_code_zero(False) is False


# ---------------------------------------------------------------------------
# 7. Client reuse + pacing + mutation guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_client_reused(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_broker(monkeypatch, contract_env, _mock_handler())
    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    summary = _summary(_parse_stdout(capsys))

    assert summary["client_instances_created"] == 1
    assert summary["from_app_settings_calls"] == 5


@pytest.mark.asyncio
async def test_client_factory_descriptor_restored_after_success(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _setup_broker(monkeypatch, contract_env, _mock_handler())
    original_descriptor = KiwoomMockClient.__dict__["from_app_settings"]

    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    _parse_stdout(capsys)

    restored_descriptor = KiwoomMockClient.__dict__["from_app_settings"]
    assert restored_descriptor is original_descriptor
    assert isinstance(restored_descriptor, classmethod)
    assert KiwoomMockClient.from_app_settings() is client


@pytest.mark.asyncio
async def test_client_factory_descriptor_restored_after_exception(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _setup_broker(monkeypatch, contract_env, _mock_handler())
    original_descriptor = KiwoomMockClient.__dict__["from_app_settings"]

    def _boom_emit(payload: dict[str, Any]) -> None:
        if payload.get("step") == "contract_sweep_start":
            raise RuntimeError("emit failed")

    monkeypatch.setattr(smoke, "_emit", _boom_emit)

    with pytest.raises(RuntimeError, match="emit failed"):
        await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)

    assert capsys.readouterr().out == ""
    restored_descriptor = KiwoomMockClient.__dict__["from_app_settings"]
    assert restored_descriptor is original_descriptor
    assert isinstance(restored_descriptor, classmethod)
    assert KiwoomMockClient.from_app_settings() is client


@pytest.mark.asyncio
async def test_client_factory_descriptor_restored_after_cancellation(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _setup_broker(monkeypatch, contract_env, _mock_handler())
    original_descriptor = KiwoomMockClient.__dict__["from_app_settings"]

    async def cancelling_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await smoke.run_contract_sweep(_contract_args(), pacing_fn=cancelling_sleep)

    restored_descriptor = KiwoomMockClient.__dict__["from_app_settings"]
    assert restored_descriptor is original_descriptor
    assert isinstance(restored_descriptor, classmethod)
    assert KiwoomMockClient.from_app_settings() is client


@pytest.mark.asyncio
async def test_pacing_injectable(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_broker(monkeypatch, contract_env, _mock_handler())
    pacing_count = 0

    async def counting_sleep(_s: float) -> None:
        nonlocal pacing_count
        pacing_count += 1

    await smoke.run_contract_sweep(_contract_args(), pacing_fn=counting_sleep)
    summary = _summary(_parse_stdout(capsys))

    assert summary["pacing_calls"] == 3
    assert pacing_count == 3


@pytest.mark.asyncio
async def test_zero_mutations_guaranteed(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_broker(monkeypatch, contract_env, _mock_handler())
    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    assert _summary(_parse_stdout(capsys))["mutations_performed"] == 0


@pytest.mark.asyncio
async def test_mutation_guard_raises() -> None:
    raw = {
        "kiwoom_mock_get_positions": _async_noop,
        "kiwoom_mock_place_order": _async_noop,
    }
    safe = smoke._read_only_tools(raw)
    with pytest.raises(smoke.SmokeRejected, match="read-only"):
        await safe["kiwoom_mock_place_order"]()


@pytest.mark.asyncio
async def test_step_failure_does_not_mutate(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = _mock_handler(return_codes={"kt00018": 99})
    _setup_broker(monkeypatch, contract_env, handler)

    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    summary = _summary(_parse_stdout(capsys))

    assert summary["overall_pass"] is False
    assert summary["mutations_performed"] == 0
    assert rc == 2


# ---------------------------------------------------------------------------
# 8. Missing config + provenance output + KST/SHA
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_config_exit4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        smoke,
        "validate_kiwoom_mock_config",
        lambda: ["KIWOOM_MOCK_ENABLED"],
    )
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.MOCK_BASE_URL
    )
    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    assert rc == 4


@pytest.mark.asyncio
async def test_provenance_fields_present(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_broker(monkeypatch, contract_env, _mock_handler())
    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)

    for step in _steps(lines):
        prov = step.get("provenance") or {}
        assert prov.get("broker") == "kiwoom"
        assert prov.get("environment") == "mock"
        assert prov.get("account_mode") == "kiwoom_mock"
        assert prov.get("host") == "mockapi.kiwoom.com"
        assert step["provenance_mock"] is True


@pytest.mark.asyncio
async def test_kst_time_and_deploy_sha(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_broker(monkeypatch, contract_env, _mock_handler())
    await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)

    for line in lines:
        if line["step"] in ("contract_sweep_start", "contract_sweep_summary"):
            assert "kst_time" in line
            assert "deploy_sha" in line


# ---------------------------------------------------------------------------
# 9. Transport exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_exception_reported(
    contract_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _setup_broker(monkeypatch, contract_env, fail)
    rc = await smoke.run_contract_sweep(_contract_args(), pacing_fn=_noop_sleep)
    lines = _parse_stdout(capsys)
    steps = _steps(lines)

    assert all(s["pass"] is False for s in steps)
    assert _summary(lines)["overall_pass"] is False
    assert rc == 2


async def _async_noop(**_kwargs: Any) -> dict[str, Any]:
    return {"success": True}
