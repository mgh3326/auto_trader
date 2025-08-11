# data/coins_info/upbit_pairs.py
from __future__ import annotations
import time, json, os
from pathlib import Path
import httpx
import asyncio


PROJ_ROOT = Path(__file__).resolve().parents[2]  # 프로젝트 루트로 조정 필요
CACHE_DIR = PROJ_ROOT / "tmp"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE = CACHE_DIR / "upbit_pairs.json"
TTL_SEC = 24 * 3600
UPBIT_ALL = "https://api.upbit.com/v1/market/all"

# 모듈 상수(런타임에 주입)
NAME_TO_PAIR_KR: dict[str, str] = {}
PAIR_TO_NAME_KR: dict[str, str] = {}
COIN_TO_PAIR:     dict[str, str] = {}
COIN_TO_NAME_KR:  dict[str, str] = {}
COIN_TO_NAME_EN:  dict[str, str] = {}

async def _fetch_markets() -> list[dict]:
    async with httpx.AsyncClient(timeout=8) as cli:
        r = await cli.get(UPBIT_ALL, params={"isDetails": "false"})
        r.raise_for_status()
        return r.json()

def _build_maps(rows: list[dict], prefer_base="KRW") -> dict[str, dict]:
    pair_to_name_kr, name_to_pair_kr = {}, {}
    coin_to_name_kr, coin_to_name_en, coin_to_pair = {}, {}, {}
    for x in rows:
        pair = x["market"]          # "KRW-BTC"
        base, coin = pair.split("-")
        kn, en = x["korean_name"], x["english_name"]

        pair_to_name_kr[pair] = kn
        if kn not in name_to_pair_kr or base == prefer_base:
            name_to_pair_kr[kn] = pair

        if coin not in coin_to_name_kr or base == prefer_base:
            coin_to_name_kr[coin] = kn
            coin_to_name_en[coin] = en
        if coin not in coin_to_pair or base == prefer_base:
            coin_to_pair[coin] = pair

    return dict(
        NAME_TO_PAIR_KR=name_to_pair_kr,
        PAIR_TO_NAME_KR=pair_to_name_kr,
        COIN_TO_PAIR=coin_to_pair,
        COIN_TO_NAME_KR=coin_to_name_kr,
        COIN_TO_NAME_EN=coin_to_name_en,
    )

def _load_cache() -> dict | None:
    if not CACHE_FILE.exists(): return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - data.get("cached_at", 0) < TTL_SEC:
            return data["maps"]
    except Exception:
        CACHE_FILE.unlink(missing_ok=True)
    return None

def _save_cache(maps: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cached_at": time.time(), "maps": maps},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)

def _apply_to_module(maps: dict) -> None:
    globals().update(maps)

async def get_or_refresh_maps(force: bool = False) -> dict:
    if not force:
        cached = _load_cache()
        if cached:
            _apply_to_module(cached)
            return cached
    rows = await _fetch_markets()
    maps = _build_maps(rows)
    _save_cache(maps)
    _apply_to_module(maps)
    return maps

# FastAPI startup 등에서 호출하면 모듈 상수 주입 완료
async def prime_upbit_constants() -> None:
    await get_or_refresh_maps(force=False)