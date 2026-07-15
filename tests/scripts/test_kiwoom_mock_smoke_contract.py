"""ROB-898 — Kiwoom mock account-read contract sweep tests.

Verifies the ``--mode contract`` read-only sweep in ``scripts/kiwoom_mock_smoke.py``:

* Four read endpoints (kt00018, kt00001, kt00010, kt00009) are called.
* Zero broker mutations are performed (mutation tools are guarded).
* Secret/account/token values never appear in stdout.
* Live/production host detection fails closed.
* Missing config exits 4 (names only, never values).
* Step failure does not trigger mutations.
* ``return_code=20`` (capability refusal) is never treated as success.
"""

from __future__ import annotations

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


def _parse_stdout(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    raw = capsys.readouterr().out.strip()
    return [json.loads(line) for line in raw.splitlines() if line]


def _mock_handler(
    *,
    return_codes: dict[str, int] | None = None,
    return_msg_override: str | None = None,
) -> Any:
    rc_map = return_codes or {}
    msg = return_msg_override

    def handler(request: httpx.Request) -> httpx.Response:
        api_id = request.headers.get("api-id", "")
        rc = rc_map.get(api_id, 0)
        base: dict[str, Any] = {
            "return_code": rc,
            "return_msg": msg or ("정상" if rc == 0 else "오류"),
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
        app_key="test-app-key",
        app_secret="test-app-secret",
        account_no="12345678",
    )
    transport = httpx.MockTransport(handler)
    client.set_transport_for_test(transport, token="test-bearer-token")
    return client


@pytest.fixture
def contract_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(kvar, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.MOCK_BASE_URL
    )


@pytest.fixture
def mock_broker(
    monkeypatch: pytest.MonkeyPatch, contract_env: None
) -> KiwoomMockClient:
    client = _make_mock_client(_mock_handler())
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )
    return client


def _contract_args() -> Any:
    return smoke.build_parser().parse_args(["--mode", "contract"])


def _summary(lines: list[dict[str, Any]]) -> dict[str, Any]:
    return [ln for ln in lines if ln["step"] == "contract_sweep_summary"][0]


def _steps(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ln for ln in lines if ln["step"] == "contract_step"]


# ---------------------------------------------------------------------------
# 1. Four endpoints called + step-by-step reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_calls_four_read_endpoints(
    mock_broker: KiwoomMockClient, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = await smoke.run_contract_sweep(_contract_args())

    lines = _parse_stdout(capsys)
    step_names = [ln["step"] for ln in lines]
    assert "contract_sweep_start" in step_names
    assert "contract_sweep_summary" in step_names

    contract_steps = _steps(lines)
    assert len(contract_steps) == 4

    api_ids = {ln["expected_api_id"] for ln in contract_steps}
    assert api_ids == {"kt00018", "kt00001", "kt00010", "kt00009"}

    stages = [ln["stage"] for ln in contract_steps]
    assert stages == ["positions", "deposit", "orderable_amount", "order_history"]

    summary = _summary(lines)
    assert summary["overall_pass"] is True
    assert summary["passed"] == 4
    assert summary["failed"] == 0
    assert rc == 0


@pytest.mark.asyncio
async def test_sweep_emits_kst_time_and_deploy_sha(
    mock_broker: KiwoomMockClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)

    for line in lines:
        if line["step"] in ("contract_sweep_start", "contract_sweep_summary"):
            assert "kst_time" in line
            assert "deploy_sha" in line
            assert len(line["kst_time"]) > 0
        if line["step"] == "contract_step":
            assert "kst_time" in line
            assert "deploy_sha" in line


# ---------------------------------------------------------------------------
# 2. Zero mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_zero_mutations_in_output(
    mock_broker: KiwoomMockClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    assert _summary(lines)["mutations_performed"] == 0


@pytest.mark.asyncio
async def test_mutation_guard_raises_on_access() -> None:
    raw_tools = {
        "kiwoom_mock_get_positions": _async_noop,
        "kiwoom_mock_place_order": _async_noop,
        "kiwoom_mock_cancel_order": _async_noop,
        "kiwoom_mock_modify_order": _async_noop,
    }
    safe = smoke._read_only_tools(raw_tools)
    assert safe["kiwoom_mock_get_positions"] is raw_tools["kiwoom_mock_get_positions"]
    assert safe["kiwoom_mock_place_order"] is not raw_tools["kiwoom_mock_place_order"]

    with pytest.raises(smoke.SmokeRejected, match="read-only"):
        await safe["kiwoom_mock_place_order"]()

    with pytest.raises(smoke.SmokeRejected, match="read-only"):
        await safe["kiwoom_mock_cancel_order"]()

    with pytest.raises(smoke.SmokeRejected, match="read-only"):
        await safe["kiwoom_mock_modify_order"]()


@pytest.mark.asyncio
async def test_step_failure_does_not_trigger_mutations(
    monkeypatch: pytest.MonkeyPatch,
    contract_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _make_mock_client(_mock_handler(return_codes={"kt00018": 99}))
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )

    rc = await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    summary = _summary(lines)

    assert summary["overall_pass"] is False
    assert summary["mutations_performed"] == 0
    assert rc == 2


# ---------------------------------------------------------------------------
# 3. Secret / account / token redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_never_in_stdout(
    monkeypatch: pytest.MonkeyPatch,
    contract_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _make_mock_client(
        _mock_handler(
            return_msg_override=f"error token={SENTINEL_TOKEN} secret={SENTINEL_SECRET}",
        )
    )
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )

    await smoke.run_contract_sweep(_contract_args())
    output = capsys.readouterr().out

    assert SENTINEL_SECRET not in output
    assert SENTINEL_TOKEN not in output
    assert "test-app-key" not in output
    assert "test-app-secret" not in output
    assert "test-bearer-token" not in output
    assert "12345678" not in output


def test_sanitize_return_msg_redacts_sensitive_patterns() -> None:
    assert smoke._sanitize_return_msg("ok") == "ok"
    assert smoke._sanitize_return_msg(f"token={SENTINEL_TOKEN}") == "[SANITIZED]"
    assert smoke._sanitize_return_msg(f"secret={SENTINEL_SECRET}") == "[SANITIZED]"
    assert smoke._sanitize_return_msg("Authorization: Bearer abc") == "[SANITIZED]"
    assert smoke._sanitize_return_msg(None) == ""


def test_sanitize_return_code_whitelists_numeric() -> None:
    assert smoke._sanitize_return_code(0) == 0
    assert smoke._sanitize_return_code("0") == 0
    assert smoke._sanitize_return_code(20) == 20
    assert smoke._sanitize_return_code(None) is None
    assert smoke._sanitize_return_code("abc") == "abc"


# ---------------------------------------------------------------------------
# 4. Live host detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_host_blocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smoke, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(kvar, "validate_kiwoom_mock_config", lambda: [])
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.LIVE_BASE_URL
    )

    rc = await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)

    assert rc == 2
    preflight = [ln for ln in lines if ln["step"] == "contract_preflight"][0]
    assert preflight["ok"] is False
    assert preflight["error"] == "mock_host_verification_failed"


def test_verify_mock_host_rejects_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.LIVE_BASE_URL
    )
    assert smoke._verify_mock_host() is not None


def test_verify_mock_host_accepts_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.MOCK_BASE_URL
    )
    assert smoke._verify_mock_host() is None


# ---------------------------------------------------------------------------
# 5. Missing config exits 4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_config_exits_4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        smoke,
        "validate_kiwoom_mock_config",
        lambda: ["KIWOOM_MOCK_ENABLED", "KIWOOM_MOCK_APP_KEY"],
    )
    monkeypatch.setattr(
        smoke.settings, "kiwoom_mock_base_url", kw_constants.MOCK_BASE_URL
    )

    rc = await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    preflight = [ln for ln in lines if ln["step"] == "contract_preflight"][0]

    assert rc == 4
    assert preflight["ok"] is False
    assert "KIWOOM_MOCK_ENABLED" in preflight["missing_env_keys"]
    assert "KIWOOM_MOCK_APP_KEY" in preflight["missing_env_keys"]
    assert "test-app-key" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 6. return_code=20 never converted to success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rc20_never_treated_as_success(
    monkeypatch: pytest.MonkeyPatch,
    contract_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _make_mock_client(
        _mock_handler(
            return_codes={kw_constants.ACCOUNT_DEPOSIT_API_ID: 20},
            return_msg_override="RC9000",
        )
    )
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )

    rc = await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    deposit_step = [
        ln
        for ln in lines
        if ln.get("step") == "contract_step" and ln.get("stage") == "deposit"
    ][0]

    assert deposit_step["success"] is False
    assert deposit_step["pass"] is False
    assert deposit_step["return_code"] == 20

    summary = _summary(lines)
    assert summary["overall_pass"] is False
    assert "deposit" in summary["failed_stages"]
    assert rc == 2


@pytest.mark.asyncio
async def test_nonzero_return_code_is_failure(
    monkeypatch: pytest.MonkeyPatch,
    contract_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _make_mock_client(_mock_handler(return_codes={"kt00009": 2}))
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )

    rc = await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    history_step = [
        ln
        for ln in lines
        if ln.get("step") == "contract_step" and ln.get("stage") == "order_history"
    ][0]

    assert history_step["pass"] is False
    assert history_step["return_code"] == 2

    summary = _summary(lines)
    assert "order_history" in summary["failed_stages"]
    assert rc == 2


# ---------------------------------------------------------------------------
# 7. Transport exception is reported, not swallowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_exception_reported_as_step_failure(
    monkeypatch: pytest.MonkeyPatch,
    contract_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _make_mock_client(failing_handler)
    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: client),
    )

    rc = await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    contract_steps = _steps(lines)

    assert all(s["pass"] is False for s in contract_steps)
    summary = _summary(lines)
    assert summary["overall_pass"] is False
    assert summary["mutations_performed"] == 0
    assert rc == 2


# ---------------------------------------------------------------------------
# 8. Provenance in output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_fields_present(
    mock_broker: KiwoomMockClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await smoke.run_contract_sweep(_contract_args())
    lines = _parse_stdout(capsys)
    contract_steps = _steps(lines)

    for step in contract_steps:
        prov = step.get("provenance") or {}
        assert prov.get("broker") == "kiwoom"
        assert prov.get("environment") == "mock"
        assert prov.get("account_mode") == "kiwoom_mock"
        assert prov.get("host") == "mockapi.kiwoom.com"


async def _async_noop(**_kwargs: Any) -> dict[str, Any]:
    return {"success": True}
