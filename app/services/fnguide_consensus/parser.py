"""Pure parser for FnGuide ``CompanyInfo`` HTML (hk 6508). No I/O.

The page ships its data as embedded JavaScript objects. This module reads three
shapes only: chart blocks ``key: {"header":[...],"data":[...]}`` (or the reverse
order), flat consensus objects ``{"ACC_NM": ...}`` and report objects
``{"RPT_TITLE": ...}``. Each JSON object is decoded with ``parse_float=str`` and
``parse_int=str`` so every number keeps the page's own text; values are only
emitted as exact decimal strings after validation. Anything else stays NULL with
a closed reason, never 0.

Ported from ``finance-ref-analysis/tools/fnguide_extract.py`` (string-aware
bracket matching), tightened so a header and its data must be adjacent members
of the same object.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

PAGES: tuple[str, ...] = ("Consensus", "Invest", "Finance")

#: Serialized raw-embedded bound per snapshot. Above it the raw JSON is dropped
#: and ``raw_reason='over_bound'`` is recorded; the normalized cells still land.
RAW_EMBEDDED_MAX_BYTES = 131_072

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_NAME_RE = re.compile(r"(.+?)\(([0-9]{6})\)")
# The lookbehind anchors each key to the start of its identifier. Without it the
# pattern restarts at every letter of a long alphanumeric run (quadratic time).
_HEADER_FIRST_RE = re.compile(
    r'(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{\s*"header"\s*:\s*\['
)
_DATA_FIRST_RE = re.compile(
    r'(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{\s*"data"\s*:\s*\['
)
_ACC_OBJ_RE = re.compile(r'\{[^{}]*"ACC_NM"\s*:\s*"[^"]+"[^{}]*\}')
_REPORT_OBJ_RE = re.compile(r'\{[^{}]*"RPT_TITLE"\s*:[^{}]*\}')
_BETWEEN_MEMBERS_RE = re.compile(r"\s*,\s*")
_DECIMAL_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_INT_RE = re.compile(r"-?[0-9]+")
_DATE_RE = re.compile(r"([0-9]{4})/([0-9]{2})/([0-9]{2})")
_VAL_KEY_RE = re.compile(r"VAL([0-9]+)")
_MISSING_TOKENS = frozenset({"", "-", "--", "N/A", "n/a", "NA"})

#: Row identity priority for chart rows (the first present, non-empty string wins).
_ITEM_KEYS: tuple[str, ...] = (
    "NAME",
    "NM",
    "ACC_NM",
    "YYMM",
    "FS_YEAR",
    "TRD_DT",
    "DIVD_DT",
)
#: Extra row discriminators kept in ``item_qualifier`` (e.g. ETC=최고 on stock-index rows).
_QUALIFIER_KEYS: tuple[str, ...] = ("GRP_CD", "ETC", "ROW", "SEQ")
#: Keys that describe a row rather than carry a value column.
_META_KEYS = frozenset(
    {
        *_ITEM_KEYS,
        *_QUALIFIER_KEYS,
        "AC_CODE",
        "P_AC_CODE",
        "FORMULA",
        "LVL",
        "UNIT",
        "UNT",
        "DIGIT",
        "EP_CHK",
        "CMP_CD",
    }
)

_LOGIN_OR_MISSING_MARKERS = ("로그인", "페이지 없음")


class PageRejected(ValueError):
    """The fetched HTML is not the page we asked for. ``kind`` is a closed code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class ValueRow:
    section: str
    item_name: str
    item_code: str
    item_qualifier: str
    column_key: str
    column_label: str | None
    value_text: str | None
    value_reason: str | None
    unit: str | None
    digit: int | None


@dataclass(frozen=True)
class ReportRow:
    report_hash: str
    report_date: date | None
    broker: str | None
    analyst: str | None
    title: str | None
    source_report_id: str | None


@dataclass
class ParsedPage:
    symbol: str
    page: str
    observed_date: date
    fetched_at: datetime
    source_url: str
    page_title: str | None
    company_name: str | None
    status: str
    reason: str | None
    headers: dict[str, list[Any]]
    values: list[ValueRow]
    reports: list[ReportRow]
    raw_embedded: dict[str, Any] | None
    raw_reason: str | None
    skipped_items: int = 0
    unparsed_blocks: int = 0
    duplicate_keys: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def value_count(self) -> int:
        return len(self.values)

    @property
    def report_count(self) -> int:
        return len(self.reports)


@dataclass(frozen=True)
class ChartBlock:
    key: str
    header: list[Any]
    data: list[Any]


def to_exact_decimal(raw: Any) -> tuple[str | None, str | None]:
    """Return ``(exact_text, None)`` or ``(None, reason)`` with reason missing/unparsable."""
    if raw is None:
        return None, "missing"
    if isinstance(raw, bool) or not isinstance(raw, str):
        return None, "unparsable"
    text = raw.strip().replace(",", "")
    if text in _MISSING_TOKENS:
        return None, "missing"
    if _DECIMAL_RE.fullmatch(text):
        return text, None
    return None, "unparsable"


def _unit(raw: Any) -> str | None:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _digit(raw: Any) -> int | None:
    if isinstance(raw, str) and _INT_RE.fullmatch(raw.strip()):
        return int(raw.strip())
    return None


def _column_sort_key(key: str) -> tuple[int, str]:
    match = _VAL_KEY_RE.fullmatch(key)
    return (int(match.group(1)), key) if match else (10**6, key)


def _balanced_array(text: str, start: int) -> tuple[str | None, int]:
    """Return ``(text_of_[...], end)`` for the ``[`` at ``start``, string-aware."""
    depth = 0
    in_string = False
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1
        index += 1
    return None, len(text)


def _decode(fragment: str) -> Any:
    return json.loads(fragment, parse_float=str, parse_int=str)


def parse_charts(text: str) -> tuple[dict[str, ChartBlock], int]:
    """Return ``({key: ChartBlock}, unparsed_block_count)``; first occurrence of a key wins."""
    charts: dict[str, ChartBlock] = {}
    unparsed = 0

    for match in _HEADER_FIRST_RE.finditer(text):
        key = match.group(1)
        if key in charts:
            continue
        header_raw, header_end = _balanced_array(text, match.end() - 1)
        data_marker = text.find('"data"', header_end)
        if header_raw is None or data_marker < 0:
            unparsed += 1
            continue
        if not _BETWEEN_MEMBERS_RE.fullmatch(text[header_end:data_marker]):
            continue  # the data member belongs to something else; the data-first pass decides
        data_raw, _ = _balanced_array(text, text.index("[", data_marker))
        if data_raw is None:
            unparsed += 1
            continue
        try:
            charts[key] = ChartBlock(key, _decode(header_raw), _decode(data_raw))
        except ValueError:
            unparsed += 1

    for match in _DATA_FIRST_RE.finditer(text):
        key = match.group(1)
        if key in charts:
            continue
        data_raw, data_end = _balanced_array(text, match.end() - 1)
        header_marker = text.find('"header"', data_end)
        if data_raw is None or header_marker < 0:
            unparsed += 1
            continue
        if not _BETWEEN_MEMBERS_RE.fullmatch(text[data_end:header_marker]):
            continue
        header_raw, _ = _balanced_array(text, text.index("[", header_marker))
        if header_raw is None:
            unparsed += 1
            continue
        try:
            charts[key] = ChartBlock(key, _decode(header_raw), _decode(data_raw))
        except ValueError:
            unparsed += 1

    return charts, unparsed


def _header_labels(header: list[Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for entry in header:
        if not isinstance(entry, dict):
            continue
        key = entry.get("CD") or entry.get("ID")
        if not isinstance(key, str):
            continue
        label = entry.get("YYMM") or entry.get("NM")
        if isinstance(label, str) and label.strip():
            labels[key] = label.strip()
    return labels


def _header_ids(header: list[Any]) -> set[str]:
    ids: set[str] = set()
    for entry in header:
        if not isinstance(entry, dict):
            continue
        key = entry.get("CD") or entry.get("ID")
        if isinstance(key, str):
            ids.add(key)
    return ids


def _qualifier_text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _item_name(row: dict[str, Any]) -> str | None:
    for key in _ITEM_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _report_hash(report_date: str, broker: str, analyst: str, title: str) -> str:
    raw = "\x1f".join((report_date, broker, analyst, title))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_reports(text: str) -> list[ReportRow]:
    reports: dict[str, ReportRow] = {}
    for match in _REPORT_OBJ_RE.finditer(text):
        try:
            obj = _decode(match.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        raw_date = obj.get("DT") if isinstance(obj.get("DT"), str) else ""
        broker = obj.get("BRK_NM_KOR") if isinstance(obj.get("BRK_NM_KOR"), str) else ""
        analyst = (
            obj.get("ANL_NM_KOR") if isinstance(obj.get("ANL_NM_KOR"), str) else ""
        )
        title = obj.get("RPT_TITLE") if isinstance(obj.get("RPT_TITLE"), str) else ""
        date_match = _DATE_RE.fullmatch(raw_date.strip()) if raw_date else None
        report_date = (
            date(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
            if date_match
            else None
        )
        digest = _report_hash(raw_date, broker, analyst, title)
        if digest in reports:
            continue
        rpt_id = obj.get("RPT_ID")
        reports[digest] = ReportRow(
            report_hash=digest,
            report_date=report_date,
            broker=broker or None,
            analyst=analyst or None,
            title=title or None,
            source_report_id=str(rpt_id) if rpt_id is not None else None,
        )
    return list(reports.values())


def classify_page(text: str, symbol: str) -> tuple[str | None, str | None]:
    """Return ``(title, rejection_kind)``. ``rejection_kind`` is None for a page we accept."""
    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else ""
    if any(marker in title for marker in _LOGIN_OR_MISSING_MARKERS):
        return title or None, "login_or_missing"
    name_match = _NAME_RE.search(title)
    if name_match is None:
        return title or None, "unexpected_page"
    if name_match.group(2) != symbol:
        return title or None, "code_mismatch"
    return title, None


def _company_name(title: str | None) -> str | None:
    if not title:
        return None
    match = _NAME_RE.search(title)
    return match.group(1).strip() if match else None


def parse_page(
    text: str,
    *,
    symbol: str,
    page: str,
    observed_date: date,
    fetched_at: datetime,
    source_url: str,
) -> ParsedPage:
    """Parse one fetched page. Raises :class:`PageRejected` for a page we must not store."""
    if page not in PAGES:
        raise ValueError(f"unsupported page: {page!r}")
    title, rejection = classify_page(text, symbol)
    if rejection is not None:
        raise PageRejected(rejection)

    charts, unparsed = parse_charts(text)
    skipped = 0
    duplicates = 0
    values: list[ValueRow] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    def add(row: ValueRow) -> None:
        nonlocal duplicates
        key = (
            row.section,
            row.item_name,
            row.item_code,
            row.item_qualifier,
            row.column_key,
        )
        if key in seen:
            duplicates += 1
            return
        seen.add(key)
        values.append(row)

    # Consensus objects: flat ACC_NM rows (the same rows the consensus chart carries).
    consensus_rows: dict[str, dict[str, Any]] = {}
    for match in _ACC_OBJ_RE.finditer(text):
        try:
            obj = _decode(match.group(0))
        except ValueError:
            unparsed += 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ACC_NM"), str):
            consensus_rows.setdefault(obj["ACC_NM"], obj)

    consensus_labels: dict[str, str] = {}
    consensus_chart_keys: set[str] = set()
    for key, chart in charts.items():
        if any(isinstance(row, dict) and "ACC_NM" in row for row in chart.data):
            consensus_chart_keys.add(key)
            if not consensus_labels:
                consensus_labels = _header_labels(chart.header)

    skipped_consensus = 0
    for item, obj in consensus_rows.items():
        cells = sorted(
            (k for k in obj if _VAL_KEY_RE.fullmatch(k)), key=_column_sort_key
        )
        if not cells or all(to_exact_decimal(obj[k])[0] is None for k in cells):
            if cells:
                skipped_consensus += 1
            continue
        unit = _unit(obj.get("UNIT"))
        digit = _digit(obj.get("DIGIT"))
        for column in cells:
            text_value, reason = to_exact_decimal(obj[column])
            add(
                ValueRow(
                    section="consensus",
                    item_name=item,
                    item_code="",
                    item_qualifier="",
                    column_key=column,
                    column_label=consensus_labels.get(column),
                    value_text=text_value,
                    value_reason=reason,
                    unit=unit,
                    digit=digit,
                )
            )
    skipped += skipped_consensus

    for key, chart in charts.items():
        if key in consensus_chart_keys:
            continue
        labels = _header_labels(chart.header)
        header_ids = _header_ids(chart.header)
        for row in chart.data:
            if not isinstance(row, dict):
                skipped += 1
                continue
            item = _item_name(row)
            if item is None:
                skipped += 1
                continue
            value_keys = sorted(
                (
                    k
                    for k in row
                    if k not in _META_KEYS
                    and (_VAL_KEY_RE.fullmatch(k) or k in header_ids)
                ),
                key=_column_sort_key,
            )
            if not value_keys or all(
                to_exact_decimal(row[k])[0] is None for k in value_keys
            ):
                skipped += 1
                continue
            item_code = _qualifier_text(row.get("AC_CODE"))
            item_qualifier = "|".join(
                f"{k}={row[k].strip()}"
                for k in _QUALIFIER_KEYS
                if isinstance(row.get(k), str) and row[k].strip()
            )
            unit = _unit(row.get("UNIT")) or _unit(row.get("UNT"))
            digit = _digit(row.get("DIGIT"))
            for column in value_keys:
                text_value, reason = to_exact_decimal(row[column])
                add(
                    ValueRow(
                        section=key,
                        item_name=item,
                        item_code=item_code,
                        item_qualifier=item_qualifier,
                        column_key=column,
                        column_label=labels.get(column),
                        value_text=text_value,
                        value_reason=reason,
                        unit=unit,
                        digit=digit,
                    )
                )

    reports = _parse_reports(text) if page == "Consensus" else []

    coverage: str | None = None
    if page == "Consensus" and not any(v.section == "consensus" for v in values):
        if consensus_rows and skipped_consensus == len(consensus_rows):
            coverage = "all_consensus_values_null"
        else:
            coverage = "no_consensus_rows"
    if values:
        status, reason = "parsed", coverage
    elif coverage is not None:
        status, reason = "no_values", coverage
    elif not charts and not consensus_rows:
        status, reason = "no_values", "no_embedded_blocks"
    else:
        status, reason = "no_values", "no_values_present"

    raw_embedded: dict[str, Any] | None = {
        "charts": {k: {"header": c.header, "data": c.data} for k, c in charts.items()}
    }
    serialized = json.dumps(raw_embedded, ensure_ascii=False, separators=(",", ":"))
    raw_reason: str | None = None
    if len(serialized.encode("utf-8")) > RAW_EMBEDDED_MAX_BYTES:
        raw_embedded = None
        raw_reason = "over_bound"
    elif not charts:
        raw_embedded = None
        raw_reason = "no_embedded_blocks"

    return ParsedPage(
        symbol=symbol,
        page=page,
        observed_date=observed_date,
        fetched_at=fetched_at,
        source_url=source_url,
        page_title=title,
        company_name=_company_name(title),
        status=status,
        reason=reason,
        headers={k: c.header for k, c in charts.items()},
        values=values,
        reports=reports,
        raw_embedded=raw_embedded,
        raw_reason=raw_reason,
        skipped_items=skipped,
        unparsed_blocks=unparsed,
        duplicate_keys=duplicates,
    )
