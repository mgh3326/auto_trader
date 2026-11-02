"""The committed evidence report holds no private data, and the CLI round-trips."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from research.strategy_evidence.run import main
from research.trade_retro.export import sha256_file
from tests.research.trade_retro.retro_fixtures import (
    PRIVATE_ALIASES,
    PRIVATE_SYMBOLS,
    sample_export,
    sample_prices,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMMITTED = REPO / "docs/research/rob-1458-strategy-evidence-2026-11-02.md"
#: Symbols the policy itself names; they are not private.
PUBLIC_CODES = {"459580", "357870"}


def _dividends(root: Path) -> Path:
    entries = {}
    for market, symbol in (("us", "SGOV"), ("kr", "459580")):
        path = root / market / f"{symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("date,amount\n", encoding="utf-8")
        entries[f"{market}/{symbol}"] = {
            "file": f"{market}/{symbol}.csv",
            "rows": 0,
            "sha256": sha256_file(path),
        }
    (root / "dividends_manifest.json").write_text(json.dumps({"entries": entries}))
    return root


@pytest.fixture
def run(tmp_path):
    args = [
        "report",
        "--export", str(sample_export(tmp_path / "export")),
        "--prices", str(sample_prices(tmp_path / "prices")),
        "--dividends", str(_dividends(tmp_path / "div")),
        "--public-out", str(tmp_path / "public.md"),
        "--private-dir", str(tmp_path / "private"),
    ]  # fmt: skip
    assert main(args) == 0
    return tmp_path, args


def test_public_report_masks_accounts_and_symbols(run):
    root, _ = run
    public = (root / "public.md").read_text(encoding="utf-8")
    private = (root / "private" / "report-full.md").read_text(encoding="utf-8")
    for value in (*PRIVATE_SYMBOLS, *PRIVATE_ALIASES):
        assert value not in public
    assert any(alias in private for alias in PRIVATE_ALIASES)
    assert "## Options table" in public


def test_check_matches_and_then_detects_a_change(run):
    root, args = run
    assert main([*args, "--check"]) == 0
    path = root / "public.md"
    path.write_text(path.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert main([*args, "--check"]) == 1


def test_a_tampered_dividend_file_is_refused(run):
    root, args = run
    (root / "div" / "us" / "SGOV.csv").write_text("date,amount\n2026-06-01,9\n")
    assert main(args) == 3


def test_private_dir_inside_the_repository_is_refused(run):
    _, args = run
    inside = [*args[:-1], str(REPO / "tmp-private")]
    assert main(inside) == 2


def test_committed_report_names_no_account_or_private_symbol():
    text = COMMITTED.read_text(encoding="utf-8")
    for alias in ("kis_live", "toss_live", "upbit_live", "KRW-"):
        assert alias not in text
    codes = set(re.findall(r"(?<![0-9A-Za-z])\d{6}(?![0-9A-Za-z])", text))
    assert codes <= PUBLIC_CODES, codes - PUBLIC_CODES
    # Grouped amounts (1,234,567) are money; the report holds ratios and counts.
    assert not re.search(r"\d{1,3}(,\d{3}){2,}", text)
    assert "## Options table" in text
    for question in range(1, 7):
        assert f"## {question}. " in text
