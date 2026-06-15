"""ROB-198 — read-only Naver-style market dashboard view model."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import sentry_sdk

from app.mcp_server.tooling.analysis_tool_handlers import get_fear_greed_index_impl
from app.mcp_server.tooling.fundamentals._crypto import handle_get_kimchi_premium
from app.mcp_server.tooling.fundamentals._market_index import handle_get_market_index
from app.schemas.invest_market_dashboard import (
    MarketDashboardMetric,
    MarketDashboardResponse,
    MarketDashboardSection,
    MarketDashboardState,
)


class MarketDashboardProvider(Protocol):
    async def get_indices(self) -> dict[str, Any]: ...
    async def get_fear_greed(self) -> dict[str, Any]: ...
    async def get_kimchi_premium(self) -> dict[str, Any] | list[dict[str, Any]]: ...


class DefaultMarketDashboardProvider:
    """Read-only provider backed by existing market/index/crypto tools."""

    async def get_indices(self) -> dict[str, Any]:
        return await handle_get_market_index(symbol=None, period="day", count=1)

    async def get_fear_greed(self) -> dict[str, Any]:
        return await get_fear_greed_index_impl(days=1)

    async def get_kimchi_premium(self) -> dict[str, Any] | list[dict[str, Any]]:
        return await handle_get_kimchi_premium("BTC")


def _now() -> datetime:
    return datetime.now(UTC)


def _tone(change_pct: float | None, change: float | None = None) -> str:
    value = change_pct if change_pct is not None else change
    if value is None:
        return "unknown"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _format_number(value: Any, *, digits: int = 2) -> str | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None
    if abs(num) >= 1000:
        return f"{num:,.{digits}f}"
    return f"{num:.{digits}f}"


def _metric_from_index(
    row: dict[str, Any], *, href: str | None = None
) -> MarketDashboardMetric:
    warning = str(row.get("error")) if row.get("error") else None
    change_pct = _as_float(row.get("change_pct"))
    change = _as_float(row.get("change"))
    return MarketDashboardMetric(
        label=str(row.get("name") or row.get("symbol") or "지수"),
        value=_format_number(row.get("current")),
        change=change,
        changePct=change_pct,
        tone=_tone(change_pct, change),
        source=str(row.get("source") or "market_index"),
        symbol=str(row.get("symbol")) if row.get("symbol") else None,
        href=href,
        stale=warning is not None or row.get("current") is None,
        warning=warning,
    )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _section_state(
    metrics: list[MarketDashboardMetric], warnings: list[str]
) -> MarketDashboardState:
    if not metrics:
        return "missing"
    usable = [m for m in metrics if m.value is not None and not m.warning]
    if warnings or len(usable) < len(metrics):
        return "partial" if usable else "error"
    return "fresh"


async def _capture(
    label: str, call: Callable[[], Awaitable[Any]]
) -> tuple[Any | None, str | None]:
    try:
        with sentry_sdk.start_span(
            op="invest.market.provider",
            name=f"invest.market.{label}",
        ) as span:
            span.set_tag("provider", label)
            result = await asyncio.wait_for(call(), timeout=6)
            if isinstance(result, dict):
                span.set_data("payload_keys", sorted(str(key) for key in result.keys()))
            elif isinstance(result, list):
                span.set_data("payload_length", len(result))
            return result, None
    except Exception as exc:  # provider failures should not break /invest shell
        return None, f"{label}: {exc}"


def _build_index_sections(
    indices_payload: dict[str, Any] | None, warning: str | None, as_of: datetime
) -> tuple[list[MarketDashboardSection], list[str]]:
    warnings: list[str] = []
    if warning:
        warnings.append(warning)
    rows = (
        indices_payload.get("indices", []) if isinstance(indices_payload, dict) else []
    )
    if not isinstance(rows, list):
        rows = []
        warnings.append("market_index: unexpected provider payload")

    kr_metrics: list[MarketDashboardMetric] = []
    global_metrics: list[MarketDashboardMetric] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        metric = _metric_from_index(row)
        if sym in {"KOSPI", "KOSDAQ"}:
            kr_metrics.append(metric)
        else:
            global_metrics.append(metric)

    kr_section_warnings = [m.warning for m in kr_metrics if m.warning]
    global_section_warnings = [m.warning for m in global_metrics if m.warning]
    sections = [
        MarketDashboardSection(
            id="kr_market",
            title="국내 시장",
            subtitle="코스피·코스닥 현재가와 등락률",
            state=_section_state(kr_metrics, [w for w in kr_section_warnings if w]),
            sourceOfTruth="get_market_index(KOSPI/KOSDAQ)",
            updatedAt=as_of,
            staleAfterMinutes=20,
            metrics=kr_metrics,
            warnings=[w for w in kr_section_warnings if w],
            notes=[
                "Naver 증권 시장 홈의 국내 지수 영역을 /invest용 읽기 모델로 축약했습니다."
            ],
        ),
        MarketDashboardSection(
            id="global_indices",
            title="주요 해외 지수",
            subtitle="S&P 500·NASDAQ 등 글로벌 대표 지수",
            state=_section_state(
                global_metrics, [w for w in global_section_warnings if w]
            ),
            sourceOfTruth="get_market_index(SPX/NASDAQ)",
            updatedAt=as_of,
            staleAfterMinutes=60,
            metrics=global_metrics,
            warnings=[w for w in global_section_warnings if w],
            notes=[
                "해외 지수는 현재 yfinance 기반이며 장 시간대에 따라 지연될 수 있습니다."
            ],
        ),
    ]
    return sections, warnings


def _extract_fear_greed(
    payload: dict[str, Any] | None,
) -> tuple[str | None, float | None, str | None]:
    if not isinstance(payload, dict):
        return None, None, None
    data = payload.get("data")
    current: dict[str, Any] | None = None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            current = first
    elif isinstance(data, dict):
        current = data
    if current is None:
        current = payload
    value = current.get("value") or current.get("fear_greed")
    classification = current.get("value_classification") or current.get(
        "classification"
    )
    return (
        str(value) if value is not None else None,
        _as_float(value),
        str(classification) if classification else None,
    )


def _extract_kimchi(
    payload: dict[str, Any] | list[dict[str, Any]] | None,
) -> tuple[str | None, float | None, str | None]:
    row: dict[str, Any] | None = None
    if isinstance(payload, list) and payload:
        row = payload[0]
    elif isinstance(payload, dict):
        row = payload
    if not row:
        return None, None, None
    premium = row.get("premium_pct") or row.get("kimchi_premium") or row.get("premium")
    symbol = row.get("symbol") or row.get("market") or "BTC"
    return (_format_number(premium, digits=2), _as_float(premium), str(symbol))


def _build_macro_crypto_sections(
    fear_greed_payload: dict[str, Any] | None,
    fear_greed_warning: str | None,
    kimchi_payload: dict[str, Any] | list[dict[str, Any]] | None,
    kimchi_warning: str | None,
    as_of: datetime,
) -> tuple[list[MarketDashboardSection], list[str]]:
    warnings = [w for w in [fear_greed_warning, kimchi_warning] if w]
    fg_value, fg_numeric, fg_label = _extract_fear_greed(fear_greed_payload)
    kimchi_value, kimchi_numeric, kimchi_symbol = _extract_kimchi(kimchi_payload)

    macro_metrics = [
        MarketDashboardMetric(
            label="Crypto Fear & Greed",
            value=fg_value,
            changePct=None,
            tone="unknown",
            unit=fg_label,
            source="alternative.me",
            stale=fg_value is None,
            warning=fear_greed_warning,
        )
    ]
    crypto_metrics = [
        MarketDashboardMetric(
            label="김치 프리미엄",
            value=kimchi_value,
            changePct=kimchi_numeric,
            tone=_tone(kimchi_numeric),
            unit="%",
            source="upbit/binance",
            symbol=kimchi_symbol,
            stale=kimchi_value is None,
            warning=kimchi_warning,
        )
    ]

    sections = [
        MarketDashboardSection(
            id="fx_macro",
            title="FX·금리·원자재 준비도",
            subtitle="현재는 공포·탐욕 지표를 우선 노출하고 환율/금리/원자재 확장은 데이터 준비 상태로 표시합니다.",
            state=_section_state(macro_metrics, [w for w in [fear_greed_warning] if w]),
            sourceOfTruth="get_crypto_fear_greed",
            updatedAt=as_of if fg_value is not None else None,
            staleAfterMinutes=240,
            metrics=macro_metrics,
            warnings=[w for w in [fear_greed_warning] if w],
            notes=[
                "Naver marketindex의 환율·금리·원자재 영역은 후속 데이터 확장 후보입니다."
            ],
        ),
        MarketDashboardSection(
            id="crypto_market",
            title="가상자산 시장",
            subtitle="BTC 기준 김치 프리미엄과 크립토 심리 지표",
            state=_section_state(crypto_metrics, [w for w in [kimchi_warning] if w]),
            sourceOfTruth="get_kimchi_premium(BTC)",
            updatedAt=as_of if kimchi_value is not None else None,
            staleAfterMinutes=30,
            metrics=crypto_metrics,
            warnings=[w for w in [kimchi_warning] if w],
            notes=["투자 조언이 아닌 시장 상태 참고용 읽기 전용 지표입니다."],
        ),
    ]
    _ = fg_numeric
    return sections, warnings


def _overall_state(
    sections: list[MarketDashboardSection], warnings: list[str]
) -> MarketDashboardState:
    if not sections:
        return "missing"
    if all(section.state in {"missing", "error"} for section in sections):
        return "error"
    if warnings or any(section.state != "fresh" for section in sections):
        return "partial"
    return "fresh"


async def build_market_dashboard(
    provider: MarketDashboardProvider | None = None,
) -> MarketDashboardResponse:
    provider = provider or DefaultMarketDashboardProvider()
    as_of = _now()
    (
        (indices, index_warning),
        (fear_greed, fear_greed_warning),
        (kimchi, kimchi_warning),
    ) = await asyncio.gather(
        _capture("market_index", provider.get_indices),
        _capture("fear_greed", provider.get_fear_greed),
        _capture("kimchi_premium", provider.get_kimchi_premium),
    )

    sections, warnings = _build_index_sections(indices, index_warning, as_of)
    extra_sections, extra_warnings = _build_macro_crypto_sections(
        fear_greed, fear_greed_warning, kimchi, kimchi_warning, as_of
    )
    sections.extend(extra_sections)
    warnings.extend(extra_warnings)

    return MarketDashboardResponse(
        asOf=as_of,
        state=_overall_state(sections, warnings),
        sections=sections,
        warnings=warnings,
        notes=[
            "Naver-style market/index dashboard using existing read-only providers.",
            "No broker/order/watch-order mutations or scheduled collectors are invoked.",
        ],
    )
