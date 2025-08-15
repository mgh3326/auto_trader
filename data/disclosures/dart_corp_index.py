from __future__ import annotations

import asyncio
import os, json, time, io
import zipfile
from pathlib import Path
from typing import Dict

import dart_fss
import httpx  # dart-fss 안 쓰고 REST로 인덱스만 받을 때
import xml.etree.ElementTree as ET

import pandas as pd
from app.core.config import settings

# dart-fss를 써도 동일: 인덱스는 openDART의 corpCode.zip이 표준

TTL = 24 * 3600
CACHE_DIR = Path(os.getenv("AUTO_TRADER_CACHE_DIR", "/tmp/auto_trader"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "dart_corp_index.json"

NAME_TO_CORP: dict[str, str] = {}  # "삼성전자" -> corp_code
_t = 0.0


def _atomic_write(p: Path, obj: dict):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, p)


def _load_cache() -> dict | None:
    if not CACHE_FILE.exists(): return None
    data = json.loads(CACHE_FILE.read_text("utf-8"))
    if time.time() - data.get("cached_at", 0) < TTL:
        return data["name_to_corp"]
    return None


def _apply(mapping: dict[str, str]):
    NAME_TO_CORP.clear()
    NAME_TO_CORP.update(mapping)


async def refresh_index() -> None:
    # 공식 corpCode 다운로드/파싱 (간단화를 위해 공개 REST 예시 생략)
    # dart-fss 사용 시: dart_fss.get_corp_code() 등 활용
    # 여기선 이미 파싱된 dict를 얻었다고 가정
    mapping = await fetch_and_parse_corp_code()  # api_key 파라미터 제거
    _apply(mapping)
    _atomic_write(CACHE_FILE, {"cached_at": time.time(), "name_to_corp": mapping})


async def prime_index() -> None:
    cached = _load_cache()
    if cached:
        _apply(cached)
        return
    await refresh_index()



def _fetch_corp_index_sync() -> Dict[str, str]:
    """
    dart_fss.get_corp_code()로 전체 회사 목록을 받아
    '한글회사명 -> corp_code' 매핑을 만든다.
    동일 회사명이 여러 번 나오면 '상장사(stock_code 존재)'를 우선 채택.
    """
    api_key = settings.opendart_api_key  # settings에서 api_key 가져오기
    if not api_key:
        raise ValueError("OpenDART API Key 가 비어 있습니다.")

    dart_fss.set_api_key(api_key=api_key)

    # CorpList 객체 반환 (공식 문서)
    corp_list = dart_fss.get_corp_list()

    name_to_corp: Dict[str, str] = {}
    listed_flag: Dict[str, bool] = {}

    # corp_list.corps 는 Corp 객체 리스트
    for corp in corp_list.corps:
        name = (corp.corp_name or "").strip()
        code = (corp.corp_code or "").strip()  # ← 우리가 필요한 corp_code
        stock = (corp.stock_code or "").strip()  # 상장 여부 판단용
        if not name or not code:
            continue

        listed = bool(stock)

        # 동일 이름 여러개면 상장사 우선 교체
        if name in name_to_corp:
            if listed and not listed_flag.get(name, False):
                name_to_corp[name] = code
                listed_flag[name] = True
            continue

        name_to_corp[name] = code
        listed_flag[name] = listed

    return name_to_corp

async def fetch_and_parse_corp_code() -> dict[str, str]:
    """OpenDART corpCode를 dart-fss로 받아 회사명→corp_code 매핑 반환 (async 래퍼)."""
    return await asyncio.to_thread(_fetch_corp_index_sync)