from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = REPOSITORY_ROOT / "research" / "kr_backfill" / "sources.py"


@pytest.fixture
def sources_module() -> ModuleType:
    module_name = "kr_backfill_sources_payload_test"
    spec = importlib.util.spec_from_file_location(module_name, SOURCES_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)


class _Client:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def post_api(self, **_kwargs: Any) -> dict[str, Any]:
        return self.payload


@pytest.mark.asyncio
async def test_kiwoom_missing_trade_value_uses_close_volume_proxy(
    sources_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKFILL_DAYTIME_APPROVED", "true")
    client = _Client(
        {
            "return_code": 0,
            "stk_min_pole_chart_qry": [
                {
                    "cntr_tm": "20260724153000",
                    "open_pric": "-100",
                    "high_pric": "-101",
                    "low_pric": "-99",
                    "cur_prc": "-100",
                    "trde_qty": "10",
                    "trde_prica": None,
                }
            ],
        }
    )
    pacer = sources_module.Pacer("kiwoom")
    pacer.interval = 0.0

    rows, meta = await sources_module.fetch_kiwoom_minutes(
        client=client,
        symbol="196170",
        pacer=pacer,
        max_pages=1,
        base_dt="20260724",
    )

    assert meta["rows_raw"] == 1
    assert next(iter(rows.values())) == {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 10.0,
        "value": 1000.0,
    }


@pytest.mark.asyncio
async def test_kiwoom_provider_rejection_is_not_reported_as_empty_data(
    sources_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKFILL_DAYTIME_APPROVED", "true")
    client = _Client({"return_code": 3, "return_msg": "sensitive provider text"})
    pacer = sources_module.Pacer("kiwoom")
    pacer.interval = 0.0

    with pytest.raises(RuntimeError, match="return_code=3") as exc_info:
        await sources_module.fetch_kiwoom_minutes(
            client=client,
            symbol="196170",
            pacer=pacer,
            max_pages=1,
            base_dt="20260724",
        )

    assert "sensitive provider text" not in str(exc_info.value)
