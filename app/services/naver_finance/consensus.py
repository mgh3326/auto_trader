from __future__ import annotations

from typing import Any

import httpx

from app.services.naver_finance.parser import DEFAULT_HEADERS

NAVER_INTEGRATION_URL = "https://m.stock.naver.com/api/stock/{code}/integration"


async def fetch_analyst_consensus(code: str) -> dict[str, Any]:
    """Fetch analyst consensus from Naver mobile integration API.

    Args:
        code: 6-digit Korean stock code (e.g. "005930").

    Returns:
        Dict containing recomm_mean, price_target_mean, and warnings.
    """
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"Invalid Korean stock code: {code}")

    url = NAVER_INTEGRATION_URL.format(code=code)
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()

    warnings: list[str] = []

    consensus_info = payload.get("consensusInfo")
    if not isinstance(consensus_info, dict):
        warnings.append("missing or invalid key: consensusInfo")
        consensus_info = {}

    recomm_mean = None
    if "recommMean" in consensus_info:
        recomm_mean = consensus_info["recommMean"]
    else:
        warnings.append("missing key: recommMean")

    price_target_mean = None
    if "priceTargetMean" in consensus_info:
        price_target_mean = consensus_info["priceTargetMean"]
    else:
        warnings.append("missing key: priceTargetMean")

    return {
        "recomm_mean": recomm_mean,
        "price_target_mean": price_target_mean,
        "warnings": warnings,
    }
