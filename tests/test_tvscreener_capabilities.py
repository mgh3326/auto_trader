from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.tvscreener_service as tvscreener_service
from app.services.tvscreener_service import (
    TvScreenerCapabilityState,
    TvScreenerMalformedRequestError,
    TvScreenerRateLimitError,
    TvScreenerService,
)


def _fake_stock_module(**field_overrides: object) -> SimpleNamespace:
    stock_field_attrs: dict[str, object] = {
        "NAME": "name",
        "VOLUME": "volume",
        "CHANGE_PERCENT": "change_percent",
        "RELATIVE_STRENGTH_INDEX_14": "rsi14",
        "AVERAGE_DIRECTIONAL_INDEX_14": "adx14",
    }
    stock_field_attrs.update(field_overrides)
    return SimpleNamespace(
        Market=SimpleNamespace(KOREA="KOREA", AMERICA="AMERICA"),
        StockScreener=type("FakeStockScreener", (), {}),
        StockField=type("FakeStockField", (), stock_field_attrs),
    )


@pytest.mark.asyncio
async def test_stock_capabilities_keep_empty_probe_results_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(return_value=pd.DataFrame(columns=["name", "sector"]))

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(SECTOR="sector"),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    snapshot = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )

    assert snapshot.status("sector") is TvScreenerCapabilityState.UNKNOWN
    assert snapshot.field("sector") == "sector"
    await_args = probe.await_args
    assert await_args is not None
    assert await_args.kwargs["where_clause"] is None
    assert await_args.kwargs["limit"] > 1


@pytest.mark.asyncio
async def test_stock_capabilities_keep_valueless_probe_samples_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(
        return_value=pd.DataFrame(
            {
                "name": ["AAPL", "MSFT", "NVDA"],
                "sector": [None, "", "   "],
            }
        )
    )

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(SECTOR="sector"),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    snapshot = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )

    assert snapshot.status("sector") is TvScreenerCapabilityState.UNKNOWN
    assert snapshot.field("sector") == "sector"


@pytest.mark.asyncio
async def test_stock_capabilities_keep_missing_probe_column_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(
        return_value=pd.DataFrame(
            {
                "name": ["AAPL", "MSFT", "NVDA"],
                "industry": ["Consumer Electronics", "Software", "Semiconductors"],
            }
        )
    )

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(SECTOR="sector"),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    snapshot = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )

    assert snapshot.status("sector") is TvScreenerCapabilityState.UNKNOWN
    assert snapshot.field("sector") == "sector"


@pytest.mark.asyncio
async def test_stock_capabilities_mark_missing_enum_field_unsupported_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(return_value=pd.DataFrame({"name": ["AAPL"]}))

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    snapshot = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )

    assert snapshot.status("sector") is TvScreenerCapabilityState.UNSUPPORTED
    assert snapshot.field("sector") is None
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_stock_capabilities_cache_hard_unsupported_probe_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(side_effect=TvScreenerMalformedRequestError("unknown field"))

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(SECTOR="sector"),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    first = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )
    second = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )

    assert first.status("sector") is TvScreenerCapabilityState.UNSUPPORTED
    assert second.status("sector") is TvScreenerCapabilityState.UNSUPPORTED
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_stock_capabilities_retry_after_transient_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(
        side_effect=[
            TvScreenerRateLimitError("rate limit"),
            pd.DataFrame({"name": ["AAPL"], "sector": ["Technology"]}),
        ]
    )

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(SECTOR="sector"),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    first = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )
    second = await service.get_stock_capabilities(
        market="us",
        capability_names={"sector"},
    )

    assert first.status("sector") is TvScreenerCapabilityState.UNKNOWN
    assert second.status("sector") is TvScreenerCapabilityState.USABLE
    assert second.field("sector") == "sector"
    assert probe.await_count == 2


@pytest.mark.asyncio
async def test_stock_capabilities_reuse_cached_usable_probe_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TvScreenerService(
        capability_registry=tvscreener_service._TvScreenerCapabilityRegistry()
    )
    probe = AsyncMock(
        return_value=pd.DataFrame({"name": ["005930"], "volume": [1_000.0]})
    )

    monkeypatch.setattr(
        "app.services.tvscreener_service._import_tvscreener",
        lambda: _fake_stock_module(VOLUME="volume"),
    )
    monkeypatch.setattr(service, "query_stock_screener", probe)

    first = await service.get_stock_capabilities(
        market="kr",
        capability_names={"volume"},
    )
    second = await service.get_stock_capabilities(
        market="kr",
        capability_names={"volume"},
    )

    assert first.status("volume") is TvScreenerCapabilityState.USABLE
    assert second.status("volume") is TvScreenerCapabilityState.USABLE
    assert probe.await_count == 1
