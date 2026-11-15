"""Archive parsing and checksum verification on synthetic zips (no network)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from research.crypto_trend.archive import (
    ArchiveError,
    fetch_month,
    month_bounds_ms,
    month_list,
    parse_kline_csv,
    verify_zip,
    zip_url,
)
from research.crypto_trend.http import FetchResponse

JAN_2024 = 1_704_067_200_000  # 2024-01-01T00:00:00Z
H4 = 4 * 3_600_000


def _csv(rows: list[str], header: bool = False) -> str:
    head = ["open_time,open,high,low,close,volume"] if header else []
    return "\n".join(head + rows) + "\n"


def _row(open_ms: int, close: str = "101") -> str:
    return f"{open_ms},100,102,99,{close},5,0,0,10,0,0,0"


def _zip(text: str, name: str = "BTCUSDT-4h-2024-01.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, text)
    return buf.getvalue()


def _checksum(body: bytes, name: str) -> str:
    return f"{hashlib.sha256(body).hexdigest()}  {name}\n"


@pytest.mark.unit
def test_month_list_and_bounds() -> None:
    assert month_list("2023-11", "2024-02") == ["2023-11", "2023-12", "2024-01"]
    lo, hi = month_bounds_ms("2024-02")
    assert lo == JAN_2024 + 31 * 24 * 3_600_000
    assert hi - lo == 29 * 24 * 3_600_000  # leap February


@pytest.mark.unit
def test_zip_url_uses_the_futures_um_monthly_path() -> None:
    assert zip_url("ETHUSDT", "4h", "2025-03") == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "ETHUSDT/4h/ETHUSDT-4h-2025-03.zip"
    )


@pytest.mark.unit
def test_parse_skips_header_and_normalises_microseconds() -> None:
    text = _csv([_row(JAN_2024), _row(JAN_2024 + H4, close="103")], header=True)
    bars = parse_kline_csv(text)
    assert [b.open_ms for b in bars] == [JAN_2024, JAN_2024 + H4]
    assert str(bars[1].close) == "103"

    micro = _csv([f"{JAN_2024 * 1000}" + ",100,102,99,101,5,0,0,10,0,0,0"])
    assert parse_kline_csv(micro)[0].open_ms == JAN_2024


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        _csv([_row(JAN_2024 + H4), _row(JAN_2024)]),  # not increasing
        _csv([_row(JAN_2024), _row(JAN_2024)]),  # duplicate open time
        _csv(["1704067200000,100,0,99,101,5,0,0,10,0,0,0"]),  # zero high
        _csv(["1704067200000,100,NaN,99,101,5,0,0,10,0,0,0"]),  # non-finite
        _csv(["1704067200000,100,102"]),  # too short
    ],
)
def test_parse_refuses_malformed_kline_rows(text: str) -> None:
    with pytest.raises(ArchiveError):
        parse_kline_csv(text)


@pytest.mark.unit
def test_verify_zip_requires_matching_checksum_and_one_csv() -> None:
    name = "BTCUSDT-4h-2024-01.zip"
    body = _zip(_csv([_row(JAN_2024)]))
    assert "100" in verify_zip(body, _checksum(body, name), name)

    with pytest.raises(ArchiveError, match="checksum mismatch"):
        verify_zip(body + b"x", _checksum(body, name), name)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("a.csv", "1,1,1,1,1\n")
        archive.writestr("b.csv", "1,1,1,1,1\n")
    two = buf.getvalue()
    with pytest.raises(ArchiveError, match="expected one CSV"):
        verify_zip(two, _checksum(two, name), name)


@pytest.mark.unit
def test_fetch_month_caches_verified_files_and_refuses_unverified_reuse(
    tmp_path: Path,
) -> None:
    name = "BTCUSDT-4h-2024-01.zip"
    body = _zip(_csv([_row(JAN_2024 + k * H4) for k in range(6)]))
    files = {
        zip_url("BTCUSDT", "4h", "2024-01"): body,
        zip_url("BTCUSDT", "4h", "2024-01") + ".CHECKSUM": _checksum(
            body, name
        ).encode(),
    }

    def fake(url: str) -> FetchResponse:
        return (
            FetchResponse(200, files[url]) if url in files else FetchResponse(404, b"")
        )

    first = fetch_month(fake, tmp_path, "BTCUSDT", "4h", "2024-01")
    assert len(first.bars) == 6 and not first.from_cache
    assert first.zip_sha256 == hashlib.sha256(body).hexdigest()

    again = fetch_month(None, tmp_path, "BTCUSDT", "4h", "2024-01")
    assert again.from_cache and len(again.bars) == 6

    cached = tmp_path / "futures_um" / "4h" / "BTCUSDT" / name
    cached.write_bytes(body + b"corrupt")
    with pytest.raises(ArchiveError, match="not cached and no fetcher"):
        fetch_month(None, tmp_path, "BTCUSDT", "4h", "2024-01")


@pytest.mark.unit
def test_fetch_month_refuses_bars_outside_the_month(tmp_path: Path) -> None:
    name = "BTCUSDT-4h-2024-01.zip"
    body = _zip(_csv([_row(JAN_2024 - H4)]))  # 2023-12-31 inside a 2024-01 file
    files = {
        zip_url("BTCUSDT", "4h", "2024-01"): body,
        zip_url("BTCUSDT", "4h", "2024-01") + ".CHECKSUM": _checksum(
            body, name
        ).encode(),
    }

    def fake(url: str) -> FetchResponse:
        return FetchResponse(200, files[url])

    with pytest.raises(ArchiveError, match="outside 2024-01"):
        fetch_month(fake, tmp_path, "BTCUSDT", "4h", "2024-01")
