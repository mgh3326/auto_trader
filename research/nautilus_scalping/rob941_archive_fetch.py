"""ROB-941 (AC1/AC4) — checksum-MANDATORY Binance public archive fetch.

Unlike ``pit_klines_fetcher`` (best-effort checksum: an archive is accepted
un-verified when its ``.CHECKSUM`` sidecar happens to be absent), the ROB-941
historical corpus is fail-closed end to end: a missing archive, a missing
checksum, a checksum mismatch, a corrupt ZIP, or a corrupt/non-UTF8 CSV member
all abort the build for that shard. Only ``data.binance.vision`` public URLs are
built here — no keys, no auth, no order endpoints.

The network I/O is a single injectable ``opener`` (``url -> bytes | None``,
``None`` meaning a confirmed-missing/404 URL) so every fail-closed path is unit-
testable with an in-memory fixture table; the default ``urllib_opener`` is only
exercised by the opt-in live test.
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

BASE = "https://data.binance.vision/data"

Opener = Callable[[str], "bytes | None"]


class ArchiveMissingError(RuntimeError):
    """The archive ZIP itself does not exist upstream (404)."""


class ChecksumMissingError(RuntimeError):
    """The mandatory ``.CHECKSUM`` sidecar is absent — fail-closed, never skip verification."""


class ChecksumMismatchError(RuntimeError):
    """The downloaded ZIP's SHA-256 does not match its published ``.CHECKSUM``."""


class CorruptArchiveError(RuntimeError):
    """The ZIP is unreadable, does not contain exactly one member, or the member is not valid UTF-8."""


def urllib_opener(url: str, timeout: int = 60) -> bytes | None:
    """Real network opener: returns bytes, ``None`` for a 404, raises otherwise."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (public data.binance.vision only)
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def kline_archive_url(
    symbol: str, interval: str, year: int, month: int, market: str = "um"
) -> str:
    stem = f"{symbol}-{interval}-{year:04d}-{month:02d}"
    return f"{BASE}/futures/{market}/monthly/klines/{symbol}/{interval}/{stem}.zip"


def funding_archive_url(symbol: str, year: int, month: int, market: str = "um") -> str:
    stem = f"{symbol}-fundingRate-{year:04d}-{month:02d}"
    return f"{BASE}/futures/{market}/monthly/fundingRate/{symbol}/{stem}.zip"


@dataclass(frozen=True)
class ArchiveProvenance:
    """Upstream URL + verified checksum for one archive — persisted in the manifest.

    The single canonical definition (``rob941_manifest`` re-exports this, it does
    not redefine it) so a value produced by the fetch layer is always the same
    type the manifest layer serializes.
    """

    url: str
    checksum_url: str
    checksum_sha256: str

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "checksum_url": self.checksum_url,
            "checksum_sha256": self.checksum_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ArchiveProvenance:
        return cls(
            url=d["url"],
            checksum_url=d["checksum_url"],
            checksum_sha256=d["checksum_sha256"],
        )


@dataclass(frozen=True)
class FetchedArchive:
    url: str
    zip_bytes: bytes
    checksum_url: str
    checksum_sha256: str  # verified to match the downloaded zip_bytes

    def provenance(self) -> ArchiveProvenance:
        return ArchiveProvenance(
            url=self.url,
            checksum_url=self.checksum_url,
            checksum_sha256=self.checksum_sha256,
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_verified_archive(url: str, opener: Opener = urllib_opener) -> FetchedArchive:
    """Download ``url`` and its mandatory ``.CHECKSUM`` sidecar; verify SHA-256.

    Fail-closed: missing archive -> ``ArchiveMissingError``; missing checksum
    sidecar -> ``ChecksumMissingError`` (never silently skip verification);
    mismatch -> ``ChecksumMismatchError``.
    """
    zip_bytes = opener(url)
    if zip_bytes is None:
        raise ArchiveMissingError(f"archive not found: {url}")
    checksum_url = f"{url}.CHECKSUM"
    checksum_bytes = opener(checksum_url)
    if checksum_bytes is None:
        raise ChecksumMissingError(
            f"missing mandatory .CHECKSUM sidecar: {checksum_url}"
        )
    expected = checksum_bytes.decode().split()[0].strip().lower()
    actual = _sha256(zip_bytes)
    if actual != expected:
        raise ChecksumMismatchError(
            f"checksum mismatch for {url}: expected {expected}, got {actual}"
        )
    return FetchedArchive(
        url=url,
        zip_bytes=zip_bytes,
        checksum_url=checksum_url,
        checksum_sha256=expected,
    )


def extract_single_csv(zip_bytes: bytes) -> str:
    """Extract the single CSV member of a Binance archive ZIP as UTF-8 text.

    Fail-closed on a corrupt ZIP, an unexpected member count, or non-UTF8 content
    — never silently pick a member or replace undecodable bytes.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            if len(names) != 1:
                raise CorruptArchiveError(
                    f"expected exactly 1 member in archive, found {len(names)}: {names}"
                )
            with zf.open(names[0]) as fh:
                raw = fh.read()
    except zipfile.BadZipFile as exc:
        raise CorruptArchiveError(f"corrupt ZIP: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptArchiveError(
            f"corrupt CSV member (not valid UTF-8): {exc}"
        ) from exc
