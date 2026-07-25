"""ROB-941 (AC1/AC4) — checksum-MANDATORY Binance public archive fetch.

Unlike ``pit_klines_fetcher`` (best-effort checksum: silently accepts an archive
when the ``.CHECKSUM`` sidecar is absent), the ROB-941 corpus is fail-closed: a
missing archive, a missing checksum, a checksum mismatch, a corrupt ZIP, or a
corrupt/non-UTF8 CSV member must all raise rather than admit unverified data.

All tests here use a fake in-memory ``opener`` (url -> bytes | None) — no real
network calls. The real ``urllib_opener`` is exercised only by the opt-in live
test (``test_rob941_corpus_builder_live.py``).
"""

import hashlib
import io
import zipfile

import pytest
import rob941_archive_fetch as af


def _zip_bytes(member_name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_name, content)
    return buf.getvalue()


def _checksum_line(zip_bytes: bytes, filename: str) -> bytes:
    digest = hashlib.sha256(zip_bytes).hexdigest()
    return f"{digest}  {filename}\n".encode()


# --------------------------------------------------------------------------- #
# URL builders (pure)
# --------------------------------------------------------------------------- #
def test_kline_archive_url_um_monthly():
    url = af.kline_archive_url("XRPUSDT", "1m", 2025, 7)
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "XRPUSDT/1m/XRPUSDT-1m-2025-07.zip"
    )


def test_funding_archive_url_um_monthly():
    url = af.funding_archive_url("XRPUSDT", 2025, 7)
    assert url == (
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
        "XRPUSDT/XRPUSDT-fundingRate-2025-07.zip"
    )


# --------------------------------------------------------------------------- #
# fail-closed fetch behavior
# --------------------------------------------------------------------------- #
def test_fetch_verified_archive_happy_path():
    zb = _zip_bytes("X-1m-2025-07.csv", b"open_time,open\n1,2\n")
    chk = _checksum_line(zb, "X-1m-2025-07.csv")
    table = {
        "https://example/x.zip": zb,
        "https://example/x.zip.CHECKSUM": chk,
    }
    fetched = af.fetch_verified_archive("https://example/x.zip", opener=table.get)
    assert fetched.zip_bytes == zb
    assert fetched.checksum_sha256 == hashlib.sha256(zb).hexdigest()


def test_fetch_verified_archive_missing_archive_is_fail_closed():
    table = {}  # 404 on the archive itself
    with pytest.raises(af.ArchiveMissingError):
        af.fetch_verified_archive("https://example/missing.zip", opener=table.get)


def test_fetch_verified_archive_missing_checksum_is_fail_closed():
    zb = _zip_bytes("X.csv", b"data")
    table = {"https://example/x.zip": zb}  # no .CHECKSUM entry -> opener returns None
    with pytest.raises(af.ChecksumMissingError):
        af.fetch_verified_archive("https://example/x.zip", opener=table.get)


def test_fetch_verified_archive_checksum_mismatch_is_fail_closed():
    zb = _zip_bytes("X.csv", b"data")
    table = {
        "https://example/x.zip": zb,
        "https://example/x.zip.CHECKSUM": b"0" * 64 + b"  X.csv\n",
    }
    with pytest.raises(af.ChecksumMismatchError):
        af.fetch_verified_archive("https://example/x.zip", opener=table.get)


def test_extract_single_csv_happy_path():
    zb = _zip_bytes("X.csv", b"a,b\n1,2\n")
    assert af.extract_single_csv(zb) == "a,b\n1,2\n"


def test_extract_single_csv_corrupt_zip_is_fail_closed():
    with pytest.raises(af.CorruptArchiveError):
        af.extract_single_csv(b"this is not a zip file at all")


def test_extract_single_csv_multi_member_zip_is_fail_closed():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "1")
        zf.writestr("b.csv", "2")
    with pytest.raises(af.CorruptArchiveError):
        af.extract_single_csv(buf.getvalue())


def test_extract_single_csv_non_utf8_member_is_fail_closed():
    zb = _zip_bytes("X.csv", b"\xff\xfe\x00\x01broken")
    with pytest.raises(af.CorruptArchiveError):
        af.extract_single_csv(zb)
