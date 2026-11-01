"""Synthetic NASDAQ Trader directory bodies for network-free tests."""

from __future__ import annotations

from datetime import date

from research.us_universe.symdir import MIN_ROWS

NASDAQ_HEADER_2026 = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
    "Round Lot Size|ETF|NextShares"
)
NASDAQ_HEADER_2008 = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size"
)
OTHER_HEADER_2026 = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
    "NASDAQ Symbol"
)
OTHER_HEADER_2008 = (
    "Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue"
)

# Enough filler rows to pass the completeness floor in every fixture body.
FILLER = tuple(f"FIL{i:04d}" for i in range(MIN_ROWS))


def footer(day: date, ncols: int) -> str:
    return f"File Creation Time: {day:%m%d%Y}17:03" + "|" * (ncols - 1)


def nasdaq_body(
    day: date, symbols: tuple[str, ...], *, old: bool = False, extra: str = ""
) -> str:
    header = NASDAQ_HEADER_2008 if old else NASDAQ_HEADER_2026
    ncols = header.count("|") + 1
    lines = [header]
    for s in sorted(set(symbols) | set(FILLER)):
        row = f"{s}|{s} Inc. - Common Stock|Q|N|N|100"
        lines.append(row if old else row + "|N|N")
    if extra:
        lines.append(extra)
    lines.append(footer(day, ncols))
    return "\r\n".join(lines) + "\r\n"


def other_body(day: date, symbols: tuple[str, ...], *, old: bool = False) -> str:
    header = OTHER_HEADER_2008 if old else OTHER_HEADER_2026
    ncols = header.count("|") + 1
    lines = [header]
    for s in sorted(set(symbols) | {f"O{f}" for f in FILLER}):
        row = f"{s}|{s} Corp Common Stock|N|{s}|N|100|N"
        lines.append(row if old else row + f"|{s}")
    lines.append(footer(day, ncols))
    return "\n".join(lines) + "\n"
