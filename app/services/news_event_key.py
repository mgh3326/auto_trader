"""Deterministic event keys so a scorer counts one event once (hk 6968).

Pure and LLM-free (hard rule 1). A key is
``ev1:{market}:{symbol}:{basis}:...`` where the basis is, in order:

1. ``doc`` (``disclosure_id``): same symbol and same disclosure identifier.
   An article qualifies only when it cites exactly one identifier (a DART
   receipt number wins over the others); a round-up citing several is not one
   event. No time window applies: the identifier is unique.
2. ``title`` (``title_day``): same symbol, same normalised title, published on
   the same calendar day in the market's own zone (KR: Asia/Seoul, US:
   America/New_York, crypto: UTC). An article with no identifier joins the
   identifier key when exactly one identifier appears among the articles with
   its title and day in the same response.
3. ``article``: its own key (URL hash). Used when the publication time is
   unknown or the normalised title is too short to identify an event.

Known limits: rewritten headlines and follow-ups on a later market day get a
different key; a corrected DART filing has a new receipt number.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

EVENT_KEY_VERSION = "ev1"
BASIS_DISCLOSURE_ID = "disclosure_id"
BASIS_TITLE_DAY = "title_day"
BASIS_ARTICLE = "article"

# Normalised titles shorter than this (spaces excluded) are too generic to merge.
MIN_TITLE_CHARS = 8

# ``source_document_ids`` field name -> identifier namespace. Generic
# ``document_id`` fields are left out: they can be per-article provider ids and
# would split one event into several.
_DISCLOSURE_ID_KINDS: dict[str, str] = {
    "rcept_no": "dart",
    "dart_id": "dart",
    "krx_id": "krx",
    "kis_id": "kis",
    "filing_id": "filing",
    "ir_id": "ir",
}
_DART_RECEIPT_RE = re.compile(r"[0-9]{14}")
_MAX_ID_CHARS = 80

# Closed list of follow-up markers that do not change which event is reported.
_MARKER = (
    r"(?:속보|단독|상보|수정|정정|재송|긴급|특보|[0-9]+\s*보|종합(?:\s*[0-9]*\s*보)?"
    r"|update[d]?(?:\s*[0-9]+)?|breaking|exclusive|corrected)"
)
_BRACKET_MARKER_RE = re.compile(rf"[\[\(【<〔]\s*{_MARKER}\s*[\]\)】>〕]")
_PREFIX_MARKER_RE = re.compile(
    r"^\s*(?:update\s*[0-9]+\s*-|(?:breaking|exclusive|updated?|속보|단독)\s*[:\-])"
)
_QUOTES_RE = re.compile(r"[\"'‘’“”「」『』]")
_SEPARATORS_RE = re.compile(r"…|\.{2,}|[·ㆍ,!?:;|]")

_MARKET_DAY_ZONE: dict[str, tzinfo] = {
    "kr": ZoneInfo("Asia/Seoul"),
    "us": ZoneInfo("America/New_York"),
    "crypto": UTC,
}


@dataclass(frozen=True)
class EventCandidate:
    title: str
    url: str
    published_at: datetime | None
    source_document_ids: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EventKey:
    key: str
    basis: str


def disclosure_key(market: str, symbol: str, kind: str, identifier: str) -> str:
    return f"{EVENT_KEY_VERSION}:{market}:{symbol}:doc:{kind}:{identifier}"


def single_disclosure_ref(source_document_ids: Any) -> tuple[str, str] | None:
    """The one ``(kind, id)`` an article is about, or None if zero or several."""
    if not isinstance(source_document_ids, Mapping):
        return None
    refs: set[tuple[str, str]] = set()
    for field, raw in source_document_ids.items():
        kind = _DISCLOSURE_ID_KINDS.get(str(field))
        if kind is None:
            continue
        for value in raw if isinstance(raw, (list, tuple)) else [raw]:
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                continue
            identifier = str(value).strip()
            if not identifier or len(identifier) > _MAX_ID_CHARS:
                continue
            if kind == "dart" and not _DART_RECEIPT_RE.fullmatch(identifier):
                continue
            refs.add((kind, identifier))
    dart = {ref for ref in refs if ref[0] == "dart"}
    chosen = dart or refs
    return next(iter(chosen)) if len(chosen) == 1 else None


def normalize_event_title(title: str | None) -> str:
    """NFKC, casefold, drop follow-up markers and punctuation noise.

    Digits, signs and ``%`` survive, so "+3%" and "-3%" stay different.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title).casefold()
    text = _BRACKET_MARKER_RE.sub(" ", text)
    while True:
        stripped = _PREFIX_MARKER_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _QUOTES_RE.sub("", text)
    text = _SEPARATORS_RE.sub(" ", text)
    return " ".join(text.split()).strip(" .")


def _market_day(value: datetime | None, market: str) -> date | None:
    if value is None:
        return None
    zone = _MARKET_DAY_ZONE.get(market, UTC)
    if value.tzinfo is None:
        # Same reading as the news store: zone-less KR values are KST.
        value = value.replace(tzinfo=zone if market == "kr" else UTC)
    return value.astimezone(zone).date()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def assign_event_keys(
    market: str, symbol: str, candidates: Sequence[EventCandidate]
) -> list[EventKey]:
    """One key per candidate, in input order."""
    refs = [single_disclosure_ref(item.source_document_ids) for item in candidates]
    buckets: list[tuple[date, str] | None] = []
    for item in candidates:
        title = normalize_event_title(item.title)
        day = _market_day(item.published_at, market)
        usable = day is not None and len(title.replace(" ", "")) >= MIN_TITLE_CHARS
        buckets.append((day, title) if usable else None)

    bucket_refs: dict[tuple[date, str], set[tuple[str, str]]] = {}
    for bucket, ref in zip(buckets, refs, strict=True):
        if bucket is not None and ref is not None:
            bucket_refs.setdefault(bucket, set()).add(ref)

    keys: list[EventKey] = []
    for item, bucket, ref in zip(candidates, buckets, refs, strict=True):
        if ref is None and bucket is not None:
            shared = bucket_refs.get(bucket, set())
            if len(shared) == 1:
                ref = next(iter(shared))
        if ref is not None:
            keys.append(
                EventKey(disclosure_key(market, symbol, *ref), BASIS_DISCLOSURE_ID)
            )
        elif bucket is not None:
            day, title = bucket
            keys.append(
                EventKey(
                    f"{EVENT_KEY_VERSION}:{market}:{symbol}:title:"
                    f"{day.isoformat()}:{_digest(title)}",
                    BASIS_TITLE_DAY,
                )
            )
        else:
            keys.append(
                EventKey(
                    f"{EVENT_KEY_VERSION}:{market}:{symbol}:article:"
                    f"{_digest(item.url or item.title)}",
                    BASIS_ARTICLE,
                )
            )
    return keys
