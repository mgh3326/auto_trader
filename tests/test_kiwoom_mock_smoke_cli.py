# tests/test_kiwoom_mock_smoke_cli.py
"""Guard tests for the Kiwoom mock smoke CLI (default-disabled, KRX-only)."""

from __future__ import annotations

import json

import pytest

from scripts import kiwoom_mock_smoke as smoke


def test_tick_aligned_price_floors_to_krx_tick():
    # 72,345 in the 50,000-200,000 band (tick 100) floors to 72,300
    assert smoke.tick_aligned_price(72345) == 72300
    # 4,321 in the 2,000-5,000 band (tick 5) floors to 4,320
    assert smoke.tick_aligned_price(4321) == 4320


def test_ensure_krx_rejects_non_krx_exchange():
    for bad in ("NXT", "SOR", "nasdaq"):
        with pytest.raises(smoke.SmokeRejected):
            smoke.ensure_krx(bad)
    assert smoke.ensure_krx("krx") == "KRX"


def test_build_parser_defaults_to_dry_run_and_no_confirm():
    parser = smoke.build_parser()
    args = parser.parse_args(
        [
            "--mode",
            "preview",
            "--symbol",
            "005930",
            "--price",
            "1000",
            "--quantity",
            "1",
        ]
    )
    assert args.confirm is False
    assert args.exchange == "KRX"


@pytest.mark.asyncio
async def test_preflight_reports_missing_keys_without_values(monkeypatch):
    monkeypatch.setattr(
        smoke,
        "validate_kiwoom_mock_config",
        lambda: ["KIWOOM_MOCK_ENABLED", "KIWOOM_MOCK_APP_KEY"],
    )
    result = await smoke.run_preflight()
    assert result["ok"] is False
    assert result["missing_env_keys"] == ["KIWOOM_MOCK_ENABLED", "KIWOOM_MOCK_APP_KEY"]


@pytest.mark.asyncio
async def test_preflight_ok_when_config_complete(monkeypatch):
    monkeypatch.setattr(smoke, "validate_kiwoom_mock_config", lambda: [])
    result = await smoke.run_preflight()
    assert result["ok"] is True
    assert result["missing_env_keys"] == []


@pytest.mark.asyncio
async def test_amain_preflight_stdout_preserves_missing_key_names(monkeypatch, capsys):
    monkeypatch.setattr(
        smoke,
        "validate_kiwoom_mock_config",
        lambda: ["KIWOOM_MOCK_APP_KEY", "KIWOOM_MOCK_APP_SECRET"],
    )
    args = smoke.build_parser().parse_args(["--mode", "preflight"])

    rc = await smoke._amain(args)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]

    assert rc == 0
    assert lines == [
        {
            "step": "preflight",
            "ok": False,
            "missing_env_keys": [
                "KIWOOM_MOCK_APP_KEY",
                "KIWOOM_MOCK_APP_SECRET",
            ],
        }
    ]


def test_extract_order_id_prefers_ord_no():
    assert smoke.extract_order_id({"ord_no": "0000111222"}) == "0000111222"
    assert smoke.extract_order_id({"order_no": "0000333444"}) == "0000333444"
    assert smoke.extract_order_id({"return_code": 0}) is None


@pytest.mark.asyncio
async def test_full_aborts_when_dry_run_fails_before_confirmed_place(monkeypatch):
    place_calls: list[dict] = []

    async def fake_preview(**kwargs):
        return {"success": True, "preview": True}

    async def fake_place(**kwargs):
        place_calls.append(kwargs)
        if kwargs.get("dry_run", True):
            return {"success": False, "error": "dry-run rejected by guard"}
        # A confirmed (dry_run=False) place must never be reached.
        return {"success": True, "ord_no": "SHOULD_NOT_HAPPEN"}

    fake_tools = {
        "kiwoom_mock_preview_order": fake_preview,
        "kiwoom_mock_place_order": fake_place,
    }
    monkeypatch.setattr(smoke, "_tools", lambda: fake_tools)

    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "005930",
            "--price",
            "50000",
            "--quantity",
            "1",
            "--confirm",
        ]
    )
    rc = await smoke.run_full(args)

    assert rc == 2
    # Only the dry-run place was attempted; no confirmed broker mutation.
    assert len(place_calls) == 1
    assert place_calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_full_cleanup_required_preserves_long_order_id(monkeypatch, capsys):
    order_id = "12345678901234567890"
    configured_secret = "secret-value-that-must-not-leak"
    configured_token = "token-value-that-must-not-leak"
    configured_account = "99001122334"

    async def fake_preview(**kwargs):
        return {"success": True, "preview": True}

    async def fake_place(**kwargs):
        if kwargs.get("dry_run", True):
            return {"success": True}
        return {"success": True, "ord_no": order_id}

    async def fake_history(**_kwargs):
        return {"success": True, "orders": []}

    async def fake_cancel(**_kwargs):
        return {"success": False, "error": "cancel_failed"}

    async def fake_positions(**_kwargs):
        return {"success": True, "positions": []}

    fake_tools = {
        "kiwoom_mock_preview_order": fake_preview,
        "kiwoom_mock_place_order": fake_place,
        "kiwoom_mock_get_order_history": fake_history,
        "kiwoom_mock_cancel_order": fake_cancel,
        "kiwoom_mock_get_positions": fake_positions,
    }
    monkeypatch.setattr(smoke, "_tools", lambda: fake_tools)
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_app_key", configured_secret)
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_app_secret", configured_secret)
    monkeypatch.setattr(smoke.settings, "kiwoom_mock_account_no", configured_account)

    args = smoke.build_parser().parse_args(
        [
            "--mode",
            "full",
            "--symbol",
            "005930",
            "--price",
            "50000",
            "--quantity",
            "1",
            "--confirm",
        ]
    )
    rc = await smoke.run_full(args)
    output = capsys.readouterr().out

    assert rc == 2
    assert order_id in output
    assert configured_secret not in output
    assert configured_token not in output
    assert configured_account not in output
