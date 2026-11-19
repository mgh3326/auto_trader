"""3795 T4d E1: the read-only ask probe CLI (no broker, no database)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.order_proposals import mandate_quote
from scripts import mandate_ask_probe

pytestmark = pytest.mark.unit


def _book(*_args) -> mandate_quote.VenueBook:
    from datetime import datetime

    from app.core.timezone import KST

    return mandate_quote.parse_kis_book(
        {
            "aspr_acpt_hour": datetime.now(KST).strftime("%H%M%S"),
            "askp1": "70100",
            "askp_rsqn1": "5",
            "bidp1": "70000",
            "bidp_rsqn1": "5",
        },
        "70050",
    )


def test_the_probe_appends_one_record_per_symbol_per_round(tmp_path, monkeypatch):
    async def book(symbol: str, code: str):
        return _book()

    monkeypatch.setattr(mandate_quote, "_read_kis_book", book)
    monkeypatch.setattr(mandate_ask_probe.asyncio, "sleep", _no_sleep)
    out = tmp_path / "e1.jsonl"
    code = mandate_ask_probe.main(
        [
            "--symbol",
            "005930",
            "--symbol",
            "000660",
            "--count",
            "3",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 6
    assert {row["outcome"] for row in rows} == {"ok"}
    assert {row["symbol"] for row in rows} == {"005930", "000660"}


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    "argv",
    [
        ["--symbol", "005930", "--count", "0"],
        ["--symbol", "005930", "--count", "501"],
        ["--symbol", "005930", "--interval-seconds", "0.1"],
        ["--symbol", "005930", "--account-mode", "toss_live"],
        ["--symbol", "005930", "--market", "equity_us"],
    ],
)
def test_the_probe_refuses_before_any_read(argv, tmp_path, monkeypatch):
    async def boom(*_args):
        raise AssertionError("must not read")

    monkeypatch.setattr(mandate_quote, "_read_kis_book", boom)
    assert mandate_ask_probe.main([*argv, "--out", str(tmp_path / "x.jsonl")]) == 2
    assert not (tmp_path / "x.jsonl").exists()


def test_the_probe_refuses_an_output_inside_the_repo(monkeypatch):
    inside = Path(mandate_ask_probe.__file__).resolve().parents[1] / "e1.jsonl"
    assert mandate_ask_probe.main(["--symbol", "005930", "--out", str(inside)]) == 2
    assert not inside.exists()


def test_the_probe_exits_nonzero_when_records_are_lost(tmp_path, monkeypatch):
    async def book(symbol: str, code: str):
        return _book()

    monkeypatch.setattr(mandate_quote, "_read_kis_book", book)
    monkeypatch.setattr(mandate_ask_probe.asyncio, "sleep", _no_sleep)
    # the output path exists as a directory: every append fails
    code = mandate_ask_probe.main(
        ["--symbol", "005930", "--count", "2", "--out", str(tmp_path)]
    )
    assert code == 1


def test_the_probe_refuses_an_output_under_a_missing_directory(tmp_path):
    out = tmp_path / "nope" / "e1.jsonl"
    assert mandate_ask_probe.main(["--symbol", "005930", "--out", str(out)]) == 2
    assert not out.parent.exists()
