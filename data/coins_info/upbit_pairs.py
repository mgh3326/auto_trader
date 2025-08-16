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

# 모듈 상수(런타임에 주입) - KRW 마켓 전용
NAME_TO_PAIR_KR: dict[str, str] = {}
PAIR_TO_NAME_KR: dict[str, str] = {}
COIN_TO_PAIR:     dict[str, str] = {}
COIN_TO_NAME_KR:  dict[str, str] = {}
COIN_TO_NAME_EN:  dict[str, str] = {}

# 성능 최적화를 위한 set 상수 - KRW 마켓 거래 가능한 코인들
KRW_TRADABLE_COINS: set[str] = set()

async def _fetch_markets() -> list[dict]:
    async with httpx.AsyncClient(timeout=8) as cli:
        r = await cli.get(UPBIT_ALL, params={"isDetails": "false"})
        r.raise_for_status()
        return r.json()

def _build_maps(rows: list[dict]) -> dict[str, dict]:
    pair_to_name_kr, name_to_pair_kr = {}, {}
    coin_to_name_kr, coin_to_name_en, coin_to_pair = {}, {}, {}
    
    # KRW 마켓만 필터링
    krw_rows = [x for x in rows if x["market"].startswith("KRW-")]
    
    for x in krw_rows:
        pair = x["market"]          # "KRW-BTC"
        base, coin = pair.split("-")
        kn, en = x["korean_name"], x["english_name"]

        pair_to_name_kr[pair] = kn
        name_to_pair_kr[kn] = pair  # KRW 마켓만 있으므로 우선순위 고려 불필요

        coin_to_name_kr[coin] = kn
        coin_to_name_en[coin] = en
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
    # 성능 최적화를 위한 set 상수 업데이트
    global KRW_TRADABLE_COINS
    KRW_TRADABLE_COINS = set(maps["COIN_TO_NAME_KR"].keys())

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