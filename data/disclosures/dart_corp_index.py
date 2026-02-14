from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import dart_fss

from app.core.config import settings

# dart-fss를 써도 동일: 인덱스는 openDART의 corpCode.zip이 표준

TTL = 24 * 3600
CACHE_DIR = Path(os.getenv("AUTO_TRADER_CACHE_DIR", "/tmp/auto_trader"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "dart_corp_index.json"

# Global indices
NAME_TO_CORP: dict[str, str] = {}  # "삼성전자" -> corp_code
STOCK_TO_CORP: dict[str, str] = {}  # "005930" -> corp_code (6-digit stock code)
CORP_TO_NAME: dict[str, str] = {}  # corp_code -> "삼성전자"

_t = 0.0

# Cache schema version - increment when structure changes
CACHE_SCHEMA_VERSION = 2


def _atomic_write(p: Path, obj: dict):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, p)


def _load_cache() -> dict | None:
    """Load cache if valid. Returns None if missing, expired, or schema mismatch."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text("utf-8"))
    except json.JSONDecodeError:
        return None

    # Check schema version - old cache without version is incomplete
    if data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None

    if time.time() - data.get("cached_at", 0) < TTL:
        return {
            "name_to_corp": data.get("name_to_corp", {}),
            "stock_to_corp": data.get("stock_to_corp", {}),
            "corp_to_name": data.get("corp_to_name", {}),
        }
    return None


def _apply(indices: dict):
    """Apply loaded indices to global variables."""
    NAME_TO_CORP.clear()
    STOCK_TO_CORP.clear()
    CORP_TO_NAME.clear()

    NAME_TO_CORP.update(indices.get("name_to_corp", {}))
    STOCK_TO_CORP.update(indices.get("stock_to_corp", {}))
    CORP_TO_NAME.update(indices.get("corp_to_name", {}))


async def refresh_index() -> None:
    """Refresh the corp index from DART and update cache."""
    indices = await fetch_and_parse_corp_code()

    # Validate indices are non-empty
    if not indices.get("name_to_corp"):
        raise ValueError("DART corp index is empty - API may have failed")
    if not indices.get("stock_to_corp"):
        raise ValueError("DART stock index is empty - corp stock mapping failed")

    _apply(indices)
    _atomic_write(
        CACHE_FILE,
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "cached_at": time.time(),
            "name_to_corp": indices["name_to_corp"],
            "stock_to_corp": indices["stock_to_corp"],
            "corp_to_name": indices["corp_to_name"],
        },
    )


async def prime_index() -> None:
    """Load index from cache if valid, otherwise refresh."""
    cached = _load_cache()
    if cached:
        _apply(cached)
        return
    await refresh_index()


def _fetch_corp_index_sync() -> dict[str, dict[str, str]]:
    """
    dart_fss.get_corp_code()로 전체 회사 목록을 받아 3개 인덱스를 구성:
    - name_to_corp: 한글회사명 -> corp_code (상장사 우선)
    - stock_to_corp: 6자리 종목코드 -> corp_code
    - corp_to_name: corp_code -> 한글회사명

    동일 회사명이 여러 번 나오면 '상장사(stock_code 존재)'를 우선 채택.
    """
    api_key = settings.opendart_api_key
    if not api_key:
        raise ValueError("OpenDART API Key 가 비어 있습니다.")

    dart_fss.set_api_key(api_key=api_key)

    corp_list = dart_fss.get_corp_list()

    name_to_corp: dict[str, str] = {}
    stock_to_corp: dict[str, str] = {}
    corp_to_name: dict[str, str] = {}
    listed_flag: dict[str, bool] = {}

    corps = getattr(corp_list, "corps", None) or []
    for corp in corps:
        name = (corp.corp_name or "").strip()
        code = (corp.corp_code or "").strip()  # 8-digit corp_code
        stock = (
            getattr(corp, "stock_code", None) or getattr(corp, "stock_id", "")
        ).strip()  # 6-digit stock code (상장 여부 판단용)

        if not name or not code:
            continue

        listed = bool(stock)

        # Build stock_to_corp mapping (for listed companies)
        if stock and len(stock) == 6:
            # Only keep first occurrence (avoid duplicates)
            if stock not in stock_to_corp:
                stock_to_corp[stock] = code

        # Build corp_to_name mapping
        corp_to_name[code] = name

        # Build name_to_corp with listed company priority
        if name in name_to_corp:
            if listed and not listed_flag.get(name, False):
                name_to_corp[name] = code
                listed_flag[name] = True
            continue

        name_to_corp[name] = code
        listed_flag[name] = listed

    return {
        "name_to_corp": name_to_corp,
        "stock_to_corp": stock_to_corp,
        "corp_to_name": corp_to_name,
    }


async def fetch_and_parse_corp_code() -> dict[str, dict[str, str]]:
    """OpenDART corpCode를 dart-fss로 받아 3개 인덱스 반환 (async 래퍼)."""
    return await asyncio.to_thread(_fetch_corp_index_sync)


def resolve_symbol(symbol: str) -> tuple[str | None, str | None]:
    """
    Resolve a symbol to (corp_code, corp_name).

    Args:
        symbol: Korean name (삼성전자), 6-digit stock code (005930), or 8-digit corp code

    Returns:
        (corp_code, corp_name) tuple, or (None, None) if not found
    """
    if not symbol:
        return None, None

    # Strip whitespace and surrounding quotes
    s = symbol.strip().strip("'\"")

    # Try as 6-digit stock code first
    if len(s) == 6 and s.isdigit():
        corp_code = STOCK_TO_CORP.get(s)
        if corp_code:
            return corp_code, CORP_TO_NAME.get(corp_code, s)
        return None, None

    # Try as 8-digit corp code
    if len(s) == 8 and s.isdigit():
        corp_name = CORP_TO_NAME.get(s)
        if corp_name:
            return s, corp_name
        return None, None

    # Try as Korean company name
    corp_code = NAME_TO_CORP.get(s)
    if corp_code:
        return corp_code, CORP_TO_NAME.get(corp_code, s)

    return None, None
