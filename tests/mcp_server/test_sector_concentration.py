import pytest

from app.mcp_server.tooling import order_validation as ov


async def _weights_ok(*, market, account_ctx):
    # semis cluster currently 800k of 10M total = 8%
    return {
        "clusters": {"semis_memory": 800_000.0},
        "total_krw": 10_000_000.0,
        "usd_krw": 1350.0,
    }


async def _cluster_semis(*, symbol, market):
    return "semis_memory"


@pytest.mark.asyncio
async def test_within_cap():
    out = await ov.evaluate_sector_concentration(
        symbol="005930",
        market="kr",
        order_estimated_value=100_000.0,
        order_currency="KRW",
        account_ctx={},
        _weights_provider=_weights_ok,
        _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "within"
    assert out["cluster"] == "semis_memory"
    assert out["cap_pct"] == 10
    assert out["fail_open"] is False
    # projected = (800k + 100k) / (10M + 100k) ~= 8.9%
    assert 8.5 < out["projected_pct"] < 9.3


@pytest.mark.asyncio
async def test_over_cap_warns_but_does_not_block():
    async def _weights_hot(*, market, account_ctx):
        return {
            "clusters": {"semis_memory": 950_000.0},
            "total_krw": 10_000_000.0,
            "usd_krw": 1350.0,
        }

    out = await ov.evaluate_sector_concentration(
        symbol="000660",
        market="kr",
        order_estimated_value=300_000.0,
        order_currency="KRW",
        account_ctx={},
        _weights_provider=_weights_hot,
        _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "over"
    assert "warning" in out
    assert out["fail_open"] is False


@pytest.mark.asyncio
async def test_crypto_fails_open():
    out = await ov.evaluate_sector_concentration(
        symbol="KRW-BTC",
        market="crypto",
        order_estimated_value=100_000.0,
        order_currency="KRW",
        account_ctx={},
        _weights_provider=_weights_ok,
        _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "unknown"
    assert out["fail_open"] is True
    assert "crypto" in out["reason"]


@pytest.mark.asyncio
async def test_unmapped_cluster_fails_open():
    async def _no_cluster(*, symbol, market):
        return None

    out = await ov.evaluate_sector_concentration(
        symbol="123456",
        market="kr",
        order_estimated_value=100_000.0,
        order_currency="KRW",
        account_ctx={},
        _weights_provider=_weights_ok,
        _cluster_resolver=_no_cluster,
    )
    assert out["verdict"] == "unknown"
    assert out["fail_open"] is True


@pytest.mark.asyncio
async def test_provider_exception_fails_open():
    async def _boom(*, market, account_ctx):
        raise RuntimeError("broker down")

    out = await ov.evaluate_sector_concentration(
        symbol="005930",
        market="kr",
        order_estimated_value=100_000.0,
        order_currency="KRW",
        account_ctx={},
        _weights_provider=_boom,
        _cluster_resolver=_cluster_semis,
    )
    assert out["verdict"] == "unknown"
    assert out["fail_open"] is True
    assert "broker down" in out["reason"]


@pytest.mark.asyncio
async def test_missing_order_value_uses_current_only():
    out = await ov.evaluate_sector_concentration(
        symbol="005930",
        market="kr",
        order_estimated_value=None,
        order_currency="KRW",
        account_ctx={},
        _weights_provider=_weights_ok,
        _cluster_resolver=_cluster_semis,
    )
    # current 8% within cap; projected omitted or equals current
    assert out["verdict"] == "within"
    assert out["current_pct"] == pytest.approx(8.0, abs=0.01)
