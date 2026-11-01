"""Pure parsers for public KIND pages and the public KIS master files.

Every parser is strict: a page that does not have the expected shape raises
``ValueError`` instead of returning a partial result.
"""

from __future__ import annotations

import html as html_lib
import io
import re
import zipfile
from datetime import date

from research.kr_delist_repair.model import KindDelistRow, KindDisclosure

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")
_SYMBOL = re.compile(r"^[0-9][0-9A-Z]{5}$")
_DELIST_HEADER = ["번호", "회사명", "종목코드", "폐지일자", "폐지사유", "비고"]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub(" ", fragment))).strip()


def parse_delist_excel(page: str, market: str) -> list[KindDelistRow]:
    """Parse the KIND 상장폐지현황 Excel (HTML table) for one market."""
    rows = [[_text(c) for c in _CELL.findall(r)] for r in _ROW.findall(page)]
    if not rows or rows[0] != _DELIST_HEADER:
        raise ValueError(f"unexpected KIND delist header: {rows[:1]!r}")
    out: list[KindDelistRow] = []
    for cells in rows[1:]:
        if len(cells) != len(_DELIST_HEADER):
            raise ValueError(f"unexpected KIND delist row: {cells!r}")
        _, name, symbol, raw_date, reason, note = cells
        if not _SYMBOL.match(symbol):
            raise ValueError(f"KIND delist row without a six-character code: {cells!r}")
        out.append(
            KindDelistRow(
                market=market,
                symbol=symbol,
                name=name,
                delist_date=date.fromisoformat(raw_date),
                reason=reason,
                note=note,
            )
        )
    return out


def parse_disclosure_search(page: str) -> list[tuple[str, str, str, str, str]]:
    """Return ``(acptno, filed_at, company, title, filer)`` rows of a search page."""
    if "조회된 결과값이 없습니다" in page:
        return []
    out: list[tuple[str, str, str, str, str]] = []
    for row in _ROW.findall(page):
        acpt = re.findall(r"openDisclsViewer\('(\d{14})'", row)
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if not acpt or len(cells) < 5:
            continue
        out.append((acpt[0], cells[1], cells[2], cells[3], cells[4]))
    if not out:
        raise ValueError(
            "KIND disclosure search page had neither rows nor an empty marker"
        )
    return out


def parse_viewer_docnos(page: str) -> list[str]:
    """Return the body document numbers listed by a KIND disclosure viewer."""
    return re.findall(r"<option value='(\d+)\|[YN]'", page)


def parse_content_url(page: str) -> str:
    urls = re.findall(r"https?://kind\.krx\.co\.kr/external/[^'\"]+\.htm", page)
    if len(set(urls)) != 1:
        raise ValueError(f"expected exactly one KIND body URL, got {sorted(set(urls))}")
    return urls[0]


def body_tokens(page: str) -> list[str]:
    page = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", page)
    raw = re.split(r"<[^>]+>", page)
    return [t for t in (_text(x) for x in raw) if t]


def parse_disclosure_body(
    page: str, acptno: str, filed_at: str
) -> list[KindDisclosure]:
    """Parse a KRX ``상장폐지`` notice body into one record per listed issue."""
    tokens = body_tokens(page)
    joined = " ".join(tokens)
    if "상장폐지" not in joined:
        raise ValueError(f"{acptno}: body is not a delisting notice")

    def after(label: str) -> str:
        for i, tok in enumerate(tokens):
            if re.sub(r"\s", "", tok).endswith(label) and i + 1 < len(tokens):
                return tokens[i + 1]
        raise ValueError(f"{acptno}: label {label!r} missing")

    raw_date = after("상장폐지일")
    match = re.search(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})", raw_date)
    if not match:
        raise ValueError(f"{acptno}: unparseable 상장폐지일 {raw_date!r}")
    delist_date = date(int(match[1]), int(match[2]), int(match[3]))
    reason = after("상장폐지사유")

    out: list[KindDisclosure] = []
    for row in _issue_rows(tokens, acptno):
        code = re.fullmatch(r"A?([0-9][0-9A-Z]{5})", row.get("단축코드", ""))
        if code is None:
            raise ValueError(f"{acptno}: issue row without a short code: {row}")
        out.append(
            KindDisclosure(
                acptno=acptno,
                symbol=code[1],
                issue_name=row.get("주권종류", ""),
                delist_date=delist_date,
                reason=reason,
                filed_at=filed_at,
            )
        )
    if not out:
        raise ValueError(f"{acptno}: no issue code in the issue table")
    return out


_ISSUE_HEADERS = {"주권종류", "주식수", "단축코드", "상장일"}


def _header(token: str) -> str | None:
    t = re.sub(r"\(.*\)|\s", "", token)
    return t if t in _ISSUE_HEADERS else None


def _issue_rows(tokens: list[str], acptno: str) -> list[dict[str, str]]:
    """Read the issue table by its header; KOSPI and KOSDAQ order columns differently."""
    try:
        start = next(i for i, t in enumerate(tokens) if _header(t) == "주권종류")
    except StopIteration as exc:
        raise ValueError(f"{acptno}: issue table header missing") from exc
    header: list[str] = []
    i = start
    while i < len(tokens) and (h := _header(tokens[i])) is not None:
        header.append(h)
        i += 1
    if "단축코드" not in header:
        raise ValueError(f"{acptno}: issue table has no 단축코드 column: {header}")
    rows: list[dict[str, str]] = []
    while i + len(header) <= len(tokens):
        chunk = tokens[i : i + len(header)]
        if re.match(r"^\d+\.", chunk[0]):
            break
        rows.append(dict(zip(header, chunk, strict=True)))
        i += len(header)
    return rows


def parse_kis_master(payload: bytes, suffix_length: int) -> dict[str, str]:
    """Parse a public KIS ``*_code.mst.zip`` into ``{symbol: name}``.

    Same fixed-width layout as ``app/services/kr_symbol_universe_service.py``
    (the source of the ``kr_symbol_universe`` table), reimplemented here so
    this research package keeps no ``app`` import.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".mst")]
        if len(members) != 1:
            raise ValueError(f"expected one .mst member, got {archive.namelist()}")
        content = archive.read(members[0]).decode("cp949")
    out: dict[str, str] = {}
    for line in content.splitlines():
        if not line.strip():
            continue
        head = line[: len(line) - suffix_length]
        symbol = head[0:9].rstrip().upper()
        if len(symbol) < 6:
            symbol = symbol.zfill(6)
        name = head[21:].strip()
        if len(symbol) == 6 and symbol.isalnum() and name:
            out[symbol] = name
    if not out:
        raise ValueError("KIS master has no symbols")
    return out
