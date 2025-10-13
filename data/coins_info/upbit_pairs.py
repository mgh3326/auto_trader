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

# ✅ Lazy loading: 필요할 때만 초기화되는 전역 변수
_upbit_maps: dict | None = None

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

async def _initialize_upbit_data() -> dict:
    """Upbit 데이터를 초기화 (캐시 우선, 없으면 API 호출)"""
    # 캐시 확인
    cached = _load_cache()
    if cached:
        return cached

    # 캐시가 없으면 API 호출
    rows = await _fetch_markets()
    maps = _build_maps(rows)
    _save_cache(maps)
    return maps


async def get_upbit_maps() -> dict:
    """
    Upbit 마켓 데이터를 반환합니다.
    최초 호출 시에만 초기화되며, 이후 호출에는 캐시된 데이터를 반환합니다.

    Returns:
        NAME_TO_PAIR_KR, PAIR_TO_NAME_KR, COIN_TO_PAIR, COIN_TO_NAME_KR, COIN_TO_NAME_EN을 포함하는 딕셔너리
    """
    global _upbit_maps
    if _upbit_maps is None:
        _upbit_maps = await _initialize_upbit_data()
    return _upbit_maps


async def get_or_refresh_maps(force: bool = False) -> dict:
    """
    Upbit 마켓 데이터를 반환하거나 강제로 갱신합니다.

    Args:
        force: True일 경우 캐시를 무시하고 API에서 새로 가져옵니다.

    Returns:
        Upbit 마켓 데이터 딕셔너리
    """
    global _upbit_maps

    if force:
        rows = await _fetch_markets()
        _upbit_maps = _build_maps(rows)
        _save_cache(_upbit_maps)
        return _upbit_maps

    return await get_upbit_maps()


# FastAPI startup 등에서 호출하면 데이터 초기화
# 호출하지 않아도 데이터 접근 시 자동으로 로드되지만,
# 시작 시 명시적으로 호출하면 초기화 시점을 제어할 수 있습니다.
async def prime_upbit_constants() -> None:
    """
    Upbit 마켓 데이터를 명시적으로 초기화합니다.

    이 함수를 호출하면 모든 마켓 데이터가 로드됩니다.
    호출하지 않아도 데이터 접근 시 자동으로 로드됩니다.
    """
    await get_upbit_maps()


# 하위 호환성을 위한 lazy dict 래퍼
class _LazyUpbitDict:
    """Lazy evaluation을 지원하는 Upbit 딕셔너리 래퍼"""

    def __init__(self, key: str):
        self._key = key

    def _get_data(self) -> dict:
        """동기적으로 데이터에 접근 (이벤트 루프 필요)"""
        global _upbit_maps
        if _upbit_maps is None:
            # 동기 컨텍스트에서는 에러 발생 - 비동기 초기화 필요
            raise RuntimeError(
                f"Upbit 데이터가 초기화되지 않았습니다. "
                f"먼저 'await upbit_pairs.prime_upbit_constants()' 또는 "
                f"'await upbit_pairs.get_upbit_maps()'를 호출하세요."
            )
        return _upbit_maps[self._key]

    def __getitem__(self, key):
        return self._get_data()[key]

    def __contains__(self, key):
        return key in self._get_data()

    def get(self, key, default=None):
        return self._get_data().get(key, default)

    def keys(self):
        return self._get_data().keys()

    def values(self):
        return self._get_data().values()

    def items(self):
        return self._get_data().items()

    def __iter__(self):
        return iter(self._get_data())

    def __len__(self):
        return len(self._get_data())

    def __repr__(self):
        return repr(self._get_data())


class _LazyUpbitSet:
    """Lazy evaluation을 지원하는 Upbit set 래퍼 (KRW_TRADABLE_COINS용)"""

    def _get_data(self) -> set:
        """동기적으로 데이터에 접근"""
        global _upbit_maps
        if _upbit_maps is None:
            raise RuntimeError(
                f"Upbit 데이터가 초기화되지 않았습니다. "
                f"먼저 'await upbit_pairs.prime_upbit_constants()' 또는 "
                f"'await upbit_pairs.get_upbit_maps()'를 호출하세요."
            )
        return set(_upbit_maps["COIN_TO_NAME_KR"].keys())

    def __contains__(self, item):
        return item in self._get_data()

    def __iter__(self):
        return iter(self._get_data())

    def __len__(self):
        return len(self._get_data())

    def __repr__(self):
        return repr(self._get_data())


# 하위 호환성: 기존 코드가 바로 사용할 수 있도록
NAME_TO_PAIR_KR = _LazyUpbitDict("NAME_TO_PAIR_KR")
PAIR_TO_NAME_KR = _LazyUpbitDict("PAIR_TO_NAME_KR")
COIN_TO_PAIR = _LazyUpbitDict("COIN_TO_PAIR")
COIN_TO_NAME_KR = _LazyUpbitDict("COIN_TO_NAME_KR")
COIN_TO_NAME_EN = _LazyUpbitDict("COIN_TO_NAME_EN")
KRW_TRADABLE_COINS = _LazyUpbitSet()