"""The committed report holds no private data and regenerates byte for byte."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from research.trade_retro.report import MARKETS, Renderer, account_labels
from research.trade_retro.run import compute, main
from tests.research.trade_retro.retro_fixtures import (
    PRIVATE_ALIASES,
    PRIVATE_SYMBOLS,
    START,
    fill_row,
    order_row,
    sample_export,
    sample_prices,
    weekdays,
    write_export,
    write_prices,
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


def test_committed_fill_denominators_add_up():
    text = COMMITTED.read_text(encoding="utf-8")
    section = text.split("## Denominators and exclusions")[1].split(
        "Orders, from every"
    )[0]
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| ") and "Market" not in line and "---" not in line
    ]
    assert [row[0] for row in rows] == ["KR", "US", "Crypto"]
    for row in rows:
        ledger, flagged, seeds, repeats, totals, proven, moved, added, used = map(
            int, row[1:]
        )
        assert (
            ledger - flagged - seeds - repeats - totals - proven - moved + added == used
        )


def test_committed_report_answers_each_data_check():
    text = COMMITTED.read_text(encoding="utf-8")
    for heading in (
        "## Data checks",
        "## Denominators and exclusions",
        "## Sensitivity",
    ):
        assert heading in text
    assert "Naive: count opening lots as buys" in text
    assert "`fill_seq`" in text


def test_headline_lists_accounts_in_label_order_when_it_differs_from_alias_order(
    tmp_path,
):
    """Pick a salt that sorts the labels against the aliases, so a raw sort would swap them."""
    salt = next(
        str(i)
        for i in range(100)
        if account_labels(["kis_live", "toss_live"], str(i))["toss_live"] == "Account A"
    )
    rows = {
        "execution_ledger": [
            fill_row(
                "kis_live",
                "kr",
                "AAA",
                "buy",
                1,
                100,
                "2026-05-04T01:00:00Z",
                "k1",
                day="2026-05-04",
            ),
            fill_row(
                "kis_live",
                "kr",
                "AAA",
                "sell",
                1,
                100,
                "2026-05-05T01:00:00Z",
                "k2",
                day="2026-05-05",
            ),
            fill_row(
                "toss_live", "kr", "AAA", "buy", 1, 100, "2026-05-04T01:00:00Z", "t1"
            ),
            fill_row(
                "kis_live",
                "kr",
                "BBB",
                "buy",
                1,
                40000,
                "2026-05-10T00:00:00Z",
                "seed",
                day="2026-05-10",
                source="manual_import",
            ),
        ]
    }
    rows["toss_live_order_ledger"] = [
        order_row(
            "toss_live",
            "kr",
            "AAA",
            "buy",
            1,
            100,
            "2026-05-04T00:50:00Z",
            "t1",
            "filled",
            filled_qty="1",
            fill_price="100",
        )
    ]
    export = write_export(tmp_path / "export", rows)
    path = export / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["salt_fingerprint"] = salt
    path.write_text(json.dumps(manifest), encoding="utf-8")
    days = weekdays(START, 40)
    flat = lambda value: [(d, value) for d in days]  # noqa: E731
    prices = write_prices(
        tmp_path / "prices",
        {
            "benchmark/kr": flat(1000.0),
            "benchmark/us": flat(1000.0),
            "benchmark/crypto": flat(1000.0),
            "kr/AAA": flat(100.0),
            "kr/BBB": flat(40000.0),
        },
    )
    results = compute(export, prices)
    renderer = Renderer(results, public=True)
    assert renderer.acct("toss_live") == "Account A"

    idle = renderer.section_idle("kr", "KRW")
    idle_rows = [
        line.split("|")
        for line in idle.splitlines()
        if re.match(r"\| Account [A-Z] \|", line)
    ]
    assert [row[1].strip() for row in idle_rows] == ["Account A", "Account B"]
    in_label_order = " / ".join(row[5].strip() for row in idle_rows)
    assert idle_rows[0][5] != idle_rows[1][5]

    headline = next(
        line
        for line in renderer.section_headline().splitlines()
        if line.startswith("| KR |")
    )
    assert in_label_order in headline
