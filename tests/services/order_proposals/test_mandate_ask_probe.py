"""3795 T4d E1: the read-only ask probe CLI (no broker, no database)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.order_proposals import mandate_quote
from scripts import mandate_ask_probe

pytestmark = pytest.mark.unit


def _book(*_args) -> mandate_quote.VenueBook:
    return mandate_quote.parse_kis_book(
        {"askp1": "70100", "askp_rsqn1": "5", "bidp1": "70000", "bidp_rsqn1": "5"},
        "70050",
    )


def test_the_probe_appends_one_record_per_symbol_per_round(tmp_path, monkeypatch):
    async def book(symbol: str, code: str):
        return _book()

    monkeypatch.setattr(mandate_quote, "_read_kis_krx", book)
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

    monkeypatch.setattr(mandate_quote, "_read_kis_krx", boom)
    assert mandate_ask_probe.main([*argv, "--out", str(tmp_path / "x.jsonl")]) == 2
    assert not (tmp_path / "x.jsonl").exists()


def test_the_probe_refuses_an_output_inside_the_repo(monkeypatch):
    inside = Path(mandate_ask_probe.__file__).resolve().parents[1] / "e1.jsonl"
    assert mandate_ask_probe.main(["--symbol", "005930", "--out", str(inside)]) == 2
    assert not inside.exists()
