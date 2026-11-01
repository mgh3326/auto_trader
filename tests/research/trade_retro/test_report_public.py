"""The committed report holds no private data and regenerates byte for byte."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from research.trade_retro.report import MARKETS, account_labels
from research.trade_retro.run import main
from tests.research.trade_retro.retro_fixtures import (
    PRIVATE_ALIASES,
    PRIVATE_SYMBOLS,
    sample_export,
    sample_prices,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMMITTED = REPO / "docs/research/rob-1439-trade-retrospective-2026-11-01.md"


@pytest.fixture
def reports(tmp_path):
    export = sample_export(tmp_path / "export")
    prices = sample_prices(tmp_path / "prices")
    args = [
        "report",
        "--export", str(export),
        "--prices", str(prices),
        "--public-out", str(tmp_path / "public.md"),
        "--private-dir", str(tmp_path / "private"),
    ]  # fmt: skip
    assert main(args) == 0
    public = (tmp_path / "public.md").read_text(encoding="utf-8")
    private = (tmp_path / "private" / "report-full.md").read_text(encoding="utf-8")
    return public, private, args


def test_public_report_masks_symbols_and_accounts_but_private_keeps_them(reports):
    public, private, _ = reports
    for value in (*PRIVATE_SYMBOLS, *PRIVATE_ALIASES):
        assert value not in public
    assert "005930" in private
    assert "kis_live" in private
    assert "Account " in public


def test_public_report_has_no_money_columns(reports):
    public, private, _ = reports
    for header in (
        "Cost basis",
        "Proceeds",
        "Opening cash (",
        "Avg NAV",
        "| Forgone |",
        "Change in",
    ):
        assert header not in public
        assert header in private


def test_amounts_appear_only_in_the_private_report(reports):
    public, private, _ = reports
    # 11 shares at 50,000 is the KR lot's cost. 7 at 100 is the US lot's.
    assert "+550,000" in private
    assert "+550,000" not in public
    assert "550,000" not in public
    assert "+700.00" in private
    assert "700.00" not in public


def test_each_market_has_sections_2_to_5_and_a_limits_section(reports):
    public, _, _ = reports
    for _market, name, _currency in MARKETS:
        for section in (
            "2. Positions",
            "3. Entry types",
            "4. Idle cash",
            "5. Largest winners and losers",
        ):
            assert f"### {name}: {section}" in public
    assert "## Data limits" in public


def test_check_mode_passes_when_unchanged_and_fails_when_edited(reports, tmp_path):
    _, _, args = reports
    assert main([*args, "--check"]) == 0
    path = tmp_path / "public.md"
    path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert main([*args, "--check"]) == 1


def test_private_dir_inside_the_repository_is_refused(tmp_path):
    export = sample_export(tmp_path / "export")
    prices = sample_prices(tmp_path / "prices")
    code = main(
        [
            "report",
            "--export",
            str(export),
            "--prices",
            str(prices),
            "--public-out",
            str(tmp_path / "public.md"),
            "--private-dir",
            str(REPO / "docs/research/private"),
        ]  # fmt: skip
    )
    assert code == 2
    assert not (REPO / "docs/research/private").exists()


def test_account_labels_are_stable_and_their_order_depends_on_the_salt():
    labels = account_labels(list(PRIVATE_ALIASES), "salt")
    assert sorted(labels.values()) == ["Account A", "Account B", "Account C"]
    assert labels == account_labels(list(reversed(PRIVATE_ALIASES)), "salt")
    orders = {
        tuple(account_labels(list(PRIVATE_ALIASES), str(i)).items()) for i in range(30)
    }
    assert len(orders) > 1


def test_committed_report_has_no_private_values():
    text = COMMITTED.read_text(encoding="utf-8")
    lowered = text.lower()
    for word in ("_live", "kis", "toss"):
        assert not re.search(rf"\b\w*{word}\w*\b", lowered), word
    # Upbit may be named only as the public price source.
    for line in text.splitlines():
        if "upbit" in line.lower():
            assert "candles" in line or "BTC/KRW" in line, line
    # Group-separated amounts and money columns would be a leak.
    assert not re.search(r"[+-]?\d{1,3}(,\d{3})+", text)
    for header in ("Cost basis", "Proceeds", "Opening cash (", "Avg NAV", "Change in"):
        assert header not in text


def test_committed_report_has_every_section_for_every_market():
    text = COMMITTED.read_text(encoding="utf-8")
    for _market, name, _currency in MARKETS:
        for section in (
            "2. Positions",
            "3. Entry types",
            "4. Idle cash",
            "5. Largest winners and losers",
        ):
            assert f"### {name}: {section}" in text
    assert "## Data limits" in text
    assert "## Reproduce" in text
