"""Tests for scripts/kis_mock_open_order_probe.py (ROB-1007).

Convention mirrors tests/test_kis_mock_us_cash_probe.py: default-disabled
gate, missing-credential-names-only reporting, and no mutation code path.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest


@pytest.fixture
def probe_module():
    return importlib.import_module("scripts.kis_mock_open_order_probe")


@pytest.mark.asyncio
async def test_disabled_gate_exits_without_calling_kis_client(
    monkeypatch, capsys, probe_module
):
    monkeypatch.delenv("KIS_MOCK_OPEN_ORDER_PROBE_ENABLED", raising=False)

    exit_code = await probe_module.run_probe(
        market="kr", start_date="20260701", end_date="20260722"
    )

    assert exit_code == 0
    assert "disabled" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_missing_mock_credentials_reports_names_not_values(
    monkeypatch, capsys, probe_module
):
    monkeypatch.setenv("KIS_MOCK_OPEN_ORDER_PROBE_ENABLED", "true")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "super-secret-app-key")
    monkeypatch.delenv("KIS_MOCK_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_MOCK_ACCOUNT_NO", raising=False)

    exit_code = await probe_module.run_probe(
        market="kr", start_date="20260701", end_date="20260722"
    )

    out = capsys.readouterr().out
    assert exit_code == 3
    assert "KIS_MOCK_APP_SECRET" in out
    assert "KIS_MOCK_ACCOUNT_NO" in out
    assert "super-secret-app-key" not in out


@pytest.mark.asyncio
async def test_success_reports_rt_cd_row_count_and_remaining_qty_flag(
    monkeypatch, capsys, probe_module
):
    monkeypatch.setenv("KIS_MOCK_OPEN_ORDER_PROBE_ENABLED", "true")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "k")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "s")
    monkeypatch.setenv("KIS_MOCK_ACCOUNT_NO", "12345678-01")

    class FakeSettings:
        kis_account_no = "12345678-01"
        kis_app_key = "k"
        kis_app_secret = "s"

    class FakeClient:
        def __init__(self, is_mock: bool) -> None:
            self._settings = FakeSettings()

        async def inquire_daily_order_domestic(self, **kwargs):
            assert kwargs["is_mock"] is True
            return [
                {
                    "odno": "0000000001",
                    "pdno": "005930",
                    "ord_qty": "10",
                    "tot_ccld_qty": "3",
                    "rmn_qty": "7",
                    "rjct_qty": "0",
                    "cncl_yn": "N",
                },
                {
                    "odno": "0000000002",
                    "pdno": "000660",
                    "ord_qty": "5",
                    "tot_ccld_qty": "5",
                    "rmn_qty": "0",
                    "rjct_qty": "0",
                    "cncl_yn": "N",
                },
            ]

    monkeypatch.setattr(
        "app.services.brokers.kis.client.KISClient", FakeClient, raising=False
    )

    exit_code = await probe_module.run_probe(
        market="kr", start_date="20260701", end_date="20260722"
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert '"rt_cd": "0"' in out
    assert '"row_count": 2' in out
    assert '"any_row_has_remaining_qty": true' in out
    assert '"order_status": "open_remaining"' in out
    assert '"order_status": "no_remaining_qty"' in out


def test_redact_row_masks_account_and_secret_values(probe_module):
    row = {
        "odno": "0000000001",
        "cano": "12345678",
        "pdno": "005930",
    }
    result = probe_module.redact_row(row, secret_values=("12345678",))

    assert result["odno"] == "0000000001"
    assert "12345678" not in str(result)


def test_probe_script_has_no_mutation_code_path(probe_module):
    """Static guard: the probe source must never reference an
    order-submit/modify/cancel client method — read-only inquiry methods
    only."""
    source = inspect.getsource(probe_module)
    forbidden_tokens = (
        "order_korea_stock",
        "sell_korea_stock",
        "cancel_korea_order",
        "modify_korea_order",
        "order_overseas_stock",
        "buy_overseas_stock",
        "sell_overseas_stock",
        "cancel_overseas_order",
        "modify_overseas_order",
        "confirm=True",
        "dry_run=False",
    )
    for token in forbidden_tokens:
        assert token not in source, f"unexpected mutation-shaped token: {token}"

    # Also confirm via AST that no attribute access even *looks* like an
    # order-mutation call anywhere in the module.
    tree = ast.parse(Path(probe_module.__file__).read_text())
    mutation_attr_names = {
        "order_korea_stock",
        "sell_korea_stock",
        "cancel_korea_order",
        "modify_korea_order",
        "order_overseas_stock",
        "buy_overseas_stock",
        "sell_overseas_stock",
        "cancel_overseas_order",
        "modify_overseas_order",
    }
    called_attrs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called_attrs & mutation_attr_names)
