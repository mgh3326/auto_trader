from __future__ import annotations

import asyncio
import datetime as dt
import enum
import fcntl
import importlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import settings

# OpenDartReader imports pandas at module level. It is resolved on first use
# instead of at import time so the ROB-1436 lazy-import MCP profiles do not
# load pandas at boot. The three names stay module attributes (PEP 562
# ``__getattr__``); a missing package resolves them to None, as before.
_OPENDARTREADER_NAMES = (
    "OpenDartReaderFactory",
    "OpenDartReaderClass",
    "OpenDartReaderCorpCodesFetcher",
)


def _load_opendartreader() -> dict[str, Any]:
    try:
        root = importlib.import_module("OpenDartReader")
        dart_module = importlib.import_module("OpenDartReader.dart")
        dart_list_module = importlib.import_module("OpenDartReader.dart_list")
    except ImportError:
        return dict.fromkeys(_OPENDARTREADER_NAMES)
    return {
        "OpenDartReaderClass": getattr(dart_module, "OpenDartReader", None),
        "OpenDartReaderFactory": getattr(root, "OpenDartReader", root),
        "OpenDartReaderCorpCodesFetcher": getattr(dart_list_module, "corp_codes", None),
    }


def _opendartreader(name: str) -> Any:
    namespace = globals()
    if name not in namespace:
        for key, value in _load_opendartreader().items():
            namespace.setdefault(key, value)
    return namespace[name]


def __getattr__(name: str) -> Any:
    if name in _OPENDARTREADER_NAMES:
        return _opendartreader(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ReportType(enum.StrEnum):
    periodic = "A"
    major_events = "B"
    issuance = "C"
    shareholding = "D"
    other = "E"


KOREAN_REPORT_TYPE_MAP: dict[str, ReportType] = {
    "정기": ReportType.periodic,
    "주요사항": ReportType.major_events,
    "발행": ReportType.issuance,
    "지분": ReportType.shareholding,
    "기타": ReportType.other,
}

_RUNTIME_CACHE_DIR = Path("/tmp/auto_trader/opendartreader")
_dart_client: Any | None = None
_dart_client_lock = asyncio.Lock()


def _error_payload(symbol: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": message,
        "filings": [],
        "symbol": symbol,
    }


def _today(today: dt.date | None = None) -> dt.date:
    return today or dt.date.today()


def _corp_codes_cache_dir() -> Path:
    docs_cache_dir = _RUNTIME_CACHE_DIR / "docs_cache"
    docs_cache_dir.mkdir(parents=True, exist_ok=True)
    return docs_cache_dir


def _corp_codes_cache_path(today: dt.date | None = None) -> Path:
    docs_cache_dir = _corp_codes_cache_dir()
    cache_name = f"opendartreader_corp_codes_{_today(today):%Y%m%d}.pkl"
    return docs_cache_dir / cache_name


def _corp_codes_lock_path() -> Path:
    return _corp_codes_cache_dir() / ".opendartreader_corp_codes.lock"


@contextmanager
def _corp_codes_cache_lock() -> Iterator[None]:
    lock_path = _corp_codes_lock_path()
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _seed_corp_codes_cache(
    api_key: str,
    today: dt.date | None = None,
) -> pd.DataFrame:
    docs_cache_dir = _corp_codes_cache_dir()

    with _corp_codes_cache_lock():
        cache_path = _corp_codes_cache_path(today=today)

        if not cache_path.exists():
            if _opendartreader("OpenDartReaderCorpCodesFetcher") is None:
                raise RuntimeError("DART functionality not available")

            fd, temp_name = tempfile.mkstemp(
                prefix=".opendartreader_corp_codes_",
                suffix=".tmp",
                dir=docs_cache_dir,
            )
            temp_path = Path(temp_name)
            os.close(fd)

            try:
                corp_codes = _opendartreader("OpenDartReaderCorpCodesFetcher")(api_key)
                corp_codes.to_pickle(temp_path)
                os.replace(temp_path, cache_path)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

        corp_codes_frame = pd.read_pickle(cache_path)

        for stale_path in docs_cache_dir.glob("opendartreader_corp_codes_*.pkl"):
            if stale_path != cache_path:
                stale_path.unlink(missing_ok=True)

        return corp_codes_frame


def _build_client(api_key: str) -> Any:
    if _opendartreader("OpenDartReaderFactory") is None:
        raise RuntimeError("DART functionality not available")

    if (
        _opendartreader("OpenDartReaderClass") is None
        or _opendartreader("OpenDartReaderCorpCodesFetcher") is None
        or _opendartreader("OpenDartReaderFactory")
        is not _opendartreader("OpenDartReaderClass")
    ):
        return _opendartreader("OpenDartReaderFactory")(api_key)

    corp_codes = _seed_corp_codes_cache(api_key)
    client = _opendartreader("OpenDartReaderClass").__new__(
        _opendartreader("OpenDartReaderClass")
    )
    client.corp_codes = corp_codes
    client.api_key = api_key
    return client


async def _get_client() -> Any | None:
    global _dart_client

    if _dart_client is not None:
        return _dart_client

    if _opendartreader("OpenDartReaderFactory") is None:
        return None

    async with _dart_client_lock:
        if _dart_client is None:
            _dart_client = await asyncio.to_thread(
                _build_client,
                settings.opendart_api_key,
            )
    return _dart_client


def _normalize_report_type(report_type: str | None) -> str | None:
    if report_type is None:
        return ""

    normalized_report_type = report_type.strip()
    if not normalized_report_type:
        return ""

    report_type_enum = KOREAN_REPORT_TYPE_MAP.get(normalized_report_type)
    if report_type_enum is None:
        return None
    return report_type_enum.value


def _row_text(row: pd.Series, key: str) -> str:
    value = row.get(key, "")
    if pd.isna(value):
        return ""
    return str(value)


def _format_receipt_date(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 8 and normalized[:8].isdigit():
        return f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:8]}"
    parsed = pd.to_datetime(normalized)
    return parsed.strftime("%Y-%m-%d")


def _normalize_filings(df: pd.DataFrame, limit: int) -> list[dict[str, str]]:
    if df.empty:
        return []

    filings: list[dict[str, str]] = []
    limit_clamped = max(limit, 0)
    for _, row in df.head(limit_clamped).iterrows():
        filings.append(
            {
                "date": _format_receipt_date(_row_text(row, "rcept_dt")),
                "report_nm": _row_text(row, "report_nm"),
                "rcp_no": _row_text(row, "rcept_no"),
                "corp_name": _row_text(row, "corp_name"),
            }
        )
    return filings


async def list_filings(
    symbol: str,
    days: int = 3,
    limit: int = 20,
    report_type: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        return _error_payload("", "symbol is required")

    if not settings.opendart_api_key:
        return _error_payload(
            normalized_symbol,
            "OPENDART_API_KEY not set. Please set environment variable.",
        )

    client = await _get_client()
    if client is None:
        return _error_payload(normalized_symbol, "DART functionality not available")

    end_date = _today()
    start_date = end_date - dt.timedelta(days=days)
    kind = _normalize_report_type(report_type)
    if kind is None:
        valid_report_types = ", ".join(KOREAN_REPORT_TYPE_MAP)
        return _error_payload(
            normalized_symbol,
            f"Invalid report_type: {report_type}. Use one of: {valid_report_types}",
        )

    def fetch_sync() -> pd.DataFrame:
        return client.list(
            corp=normalized_symbol,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            kind=kind,
            final=True,
        )

    try:
        df = await asyncio.to_thread(fetch_sync)
    except Exception as exc:
        return _error_payload(normalized_symbol, str(exc))

    return {
        "success": True,
        "filings": _normalize_filings(df, limit),
    }
