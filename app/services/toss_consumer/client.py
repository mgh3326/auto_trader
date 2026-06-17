from __future__ import annotations

from typing import Any

from app.services.toss_consumer.transport import build_toss_consumer_client


def _to_product_code(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if len(s) == 7 and s.startswith("A") and s[1:].isdigit():
        return s
    if len(s) == 6 and s.isdigit():
        return f"A{s}"
    raise ValueError(f"Invalid Korean equity symbol: {symbol}")


def _extract_envelope(payload: Any) -> Any:
    """Unwrap ``result``/``data`` envelopes if present (fail-open for drift)."""
    target = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("result"), dict):
            target = payload["result"]
        elif isinstance(payload.get("data"), dict):
            target = payload["data"]
    return target


class TossConsumerClient:
    """Live per-call fetcher for the unofficial Toss consumer API (wts-info-api).

    Each method opens and closes its own httpx client so long-running processes
    never accumulate leaked connections. Matches the naver_finance per-call
    ``async with httpx.AsyncClient(...)`` convention.
    """

    async def fetch_buy_balance(self, product_code: str) -> dict[str, Any]:
        async with build_toss_consumer_client() as client:
            response = await client.get(
                "/api/v1/stock-infos/trade/trend/trading-trend",
                params={"productCode": product_code},
            )
            response.raise_for_status()
            payload = response.json()

        target = _extract_envelope(payload)
        warnings: list[str] = []

        def _get_val(key: str) -> Any:
            if isinstance(target, dict) and key in target:
                return target[key]
            warnings.append(f"missing key: {key}")
            return None

        buy_balance_rate = _get_val("buyBalanceRate")
        sell_balance_rate = _get_val("sellBalanceRate")
        foreigner_ratio = _get_val("foreignerRatio")

        return {
            "buyBalanceRate": buy_balance_rate,
            "sellBalanceRate": sell_balance_rate,
            "foreignerRatio": foreigner_ratio,
            "warnings": warnings,
        }

    async def fetch_ai_signal(self, product_code: str) -> dict[str, Any]:
        async with build_toss_consumer_client() as client:
            response = await client.get(
                "/api/v1/dashboard/wts/overview/ai-signals/detail",
                params={"productCode": product_code, "productType": "STOCKS"},
            )
            response.raise_for_status()
            payload = response.json()

        target = _extract_envelope(payload)
        warnings: list[str] = []

        def _get_val(key: str) -> Any:
            if isinstance(target, dict) and key in target:
                return target[key]
            warnings.append(f"missing key: {key}")
            return None

        signal_direction = _get_val("signalDirection")
        reasoning = _get_val("reasoning")
        related_reasoning = _get_val("relatedReasoning")

        return {
            "signalDirection": signal_direction,
            "reasoning": reasoning,
            "relatedReasoning": related_reasoning,
            "warnings": warnings,
        }
