"""Render the evidence pack. No figure is typed by hand.

The public version is committed: it holds ratios, counts and masked account labels,
and no symbol or amount. The private version adds account names and the filled tier
orders by symbol, and stays outside the repository with the row-level CSVs.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research.strategy_evidence import averaging, losscut, rulesets
from research.strategy_evidence.common import (
    IDLE_CASH_YIELD,
    INSUFFICIENT,
    MARKET_NAMES,
    MARKETS,
    MIN_SAMPLE,
    ROUND_TRIP_COST,
    retire_verdict,
    summarize,
)
from research.strategy_evidence.evidence import (
    OPTION_VARIANTS,
    OVERLAY_ADDS,
    OVERLAY_CUTS,
    Evidence,
)
from research.strategy_evidence.parking import COST_CASES, breakeven_days
from research.trade_retro.canonical import is_parking
from research.trade_retro.report import account_labels, share, table

COMMAND = (
    "uv run python -m research.strategy_evidence.run report "
    "--export ~/work/herdr-artifacts/dots-history-2026-11-01 "
    "--prices ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 "
    "--dividends ~/work/herdr-artifacts/rob-1458-strategy-evidence/dividends-2026-11-02 "
    "--public-out docs/research/rob-1458-strategy-evidence-2026-11-02.md "
    "--private-dir ~/work/herdr-artifacts/rob-1458-strategy-evidence"
)
FETCH_COMMAND = (
    "uv run python -m research.strategy_evidence.run fetch-dividends "
    "--out <new directory>"
)


def pct(value, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:+.{digits}f}%"


def rate(value) -> str:
    """An unsigned rate, for costs and yields that are not a gain or a loss."""
    return f"{value * 100:.2f}%"


def pp(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:+.1f}pp"


def num(value, digits: int = 0) -> str:
    if value is None or (
        isinstance(value, float) and (math.isnan(value) or math.isinf(value))
    ):
        return "n/a" if value is None or math.isnan(value) else "never"
    return f"{value:.{digits}f}"


def verdict_cell(values) -> tuple[str, object]:
    s = summarize(values)
    return retire_verdict(s), s


class Renderer:
    def __init__(self, evidence: Evidence, public: bool) -> None:
        self.e = evidence
        self.public = public
        r = evidence.retro
        aliases = sorted(set(r.fills["alias"]) | set(r.orders["alias"]))
        self.labels = account_labels(aliases, r.manifest.get("salt_fingerprint", ""))

    def acct(self, alias: str) -> str:
        return self.labels[alias] if self.public else alias

    # ------------------------------------------------------------ helpers
    def _tier(self, market: str) -> dict:
        t = self.e.tier[self.e.tier["market"] == market]
        filled = t[t["filled"]]
        contract = filled[filled["in_contract"].astype(bool)] if len(filled) else filled
        mature = contract["d20"].dropna() if len(contract) else pd.Series(dtype=float)
        verdict, s = verdict_cell(mature)
        return {
            "accepted": int(t["accepted"].sum()),
            "filled": len(filled),
            "contract": len(contract),
            "mature": len(mature),
            "verdict": verdict,
            "summary": s,
            "eligible_share": float((t["lot_unrealized"].dropna() <= -0.08).mean())
            if t["lot_unrealized"].notna().any()
            else float("nan"),
            "lot_known": int(t["lot_unrealized"].notna().sum()),
        }

    def _bucket(self, market: str, bucket: str, micro: bool | None = False, since=None):
        b = self.e.buys
        b = b[(b["market"] == market) & (b["bucket"] == bucket)]
        if micro is not None:
            b = b[b["micro"] == micro]
        if since is not None:
            b = b[b["d0"] >= since]
        return b

    def _add_rule(self, market: str, threshold: float) -> pd.DataFrame:
        a = self.e.add_rule
        return a[(a["market"] == market) & (a["threshold"] == threshold)]

    def _loss(self, market: str, threshold: float, horizon: str, crossed=True):
        s = self.e.losscut
        row = s[
            (s["market"] == market)
            & np.isclose(s["threshold"], threshold)
            & (s["horizon"] == horizon)
            & (s["crossed"] == crossed)
        ]
        return None if row.empty else row.iloc[0]

    def _overlay(self, market: str, name: str) -> pd.DataFrame | None:
        return self.e.overlays.get((market, name))

    def _overlay_total(self, market: str, name: str) -> dict | None:
        """Account rows averaged with equal weight, and the base for comparison."""
        frame, base = self._overlay(market, name), self._overlay(market, "base")
        if frame is None or base is None:
            return None
        return {
            "cash_share": float(frame["cash_share"].mean()),
            "base_cash_share": float(base["cash_share"].mean()),
            "pnl": float(frame["pnl_change_pct_avg_nav"].mean()),
            "extra": float(frame["extra_cash_pct_avg_nav"].mean()),
            "dd": float(frame["max_drawdown"].min()),
            "base_dd": float(base["max_drawdown"].min()),
        }

    def _style(self, market: str, klass: str):
        s = self.e.styles
        row = s[(s["market"] == market) & (s["klass"] == klass)]
        return None if row.empty else row.iloc[0]

    # ------------------------------------------------------------ header
    def section_header(self) -> str:
        r = self.e.retro
        lines = [
            "# ROB-1458: evidence for adjusting the live buy and sell rules",
            "",
            "This committed version holds ratios, counts and masked account labels only. "
            "Symbols, amounts and account names are left out because the repository is "
            "public and the export is private trading data. The full version is outside "
            "the repository."
            if self.public
            else "Full version, with account names. Do not commit it.",
            "",
            "It states measured facts and an options table for the operator's policy "
            "decision. It recommends no security and no amount. Every number is "
            "in-sample: it is computed on the same six months any rule would be judged "
            "on, so it is a candidate to test, not a forecast.",
            "",
            "## Reproduce",
            "",
            "```bash",
            COMMAND,
            "```",
            "",
            "Add `--check` to compare both reports with a fresh computation byte for byte, "
            "without writing. It exits 3, writing nothing, if any input differs from its "
            "manifest. The dividends snapshot was made once with the command below, which "
            "uses the network and refuses a directory that already holds one.",
            "",
            "```bash",
            FETCH_COMMAND,
            "```",
            "",
            f"- Export: `{r.manifest['schema_version']}`, window "
            f"{r.manifest['window']['since_utc'][:10]} to "
            f"{r.manifest['window']['until_utc'][:10]}, `manifest.json` sha256 "
            f"`{r.manifest_sha256}`, every file checked against it, not committed.",
            f"- Prices: the ROB-1439 snapshot, `prices_manifest.json` sha256 "
            f"`{r.book.snapshot_sha256}`, not committed.",
            f"- Dividends: `dividends_manifest.json` sha256 `{self.e.dividends_sha256}` "
            "(Yahoo Finance distributions of the parking instruments), not committed.",
            "- Code: `research/strategy_evidence/`, on top of `research/trade_retro/`, whose "
            "canonical fills, orders, positions and account cash this pack reuses unchanged. "
            "Where ROB-1439 already measured something, this pack cites it and does not "
            "measure it again.",
            "",
            "**The regime.** Index return over the export window, first to last bar: "
            + "; ".join(
                f"{MARKET_NAMES[m]} {pct(v[0])} ({v[1]} to {v[2]})"
                for m, v in self.e.index_window.items()
            )
            + ". KR is one falling market, so any long rule loses there and any rule that "
            "holds cash or cuts early looks good. Read every KR number with that in mind.",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------ options
    def _options_rows(self) -> list[list[str]]:
        rows: list[list[str]] = []
        e = self.e
        # O1: keep the tier and score it.
        kr, us = self._tier("kr"), self._tier("us")
        rows.append(
            [
                "KR, US",
                "Keep `buy.underwater_support_net` as is and score it under `underwater-d20-v1` at review",
                f"Tagged buys accepted: KR {kr['accepted']}, US {us['accepted']}. Filled: KR {kr['filled']}, "
                f"US {us['filled']}. Mature D+20 inside the contract: {kr['mature'] + us['mature']} "
                f"→ {INSUFFICIENT}. Window 2026-09-08 to the last bar",
                "None measurable yet",
                f"At {share((kr['filled'] + us['filled']) / max(kr['accepted'] + us['accepted'], 1))} "
                f"fill, {MIN_SAMPLE} mature fills need roughly "
                f"{num(MIN_SAMPLE / max((kr['filled'] + us['filled']) / max(kr['accepted'] + us['accepted'], 1), 1e-9))} "
                "tagged orders; the review may never be reached",
                "The tier's own rule, unchanged: n >= 5 mature fills, keep if D+20 median >= 0 and lower quartile > -8%. "
                "Report the tagged fill rate at each review",
            ]
        )
        # O2-O3: averaging down per market. The label follows the stated test.
        for market in ("kr", "us"):
            facto = self._bucket(market, "add, lot <= -8%")
            v_facto, s_facto = verdict_cell(facto["d20"])
            add = self._add_rule(market, OVERLAY_ADDS[0])
            v_add, s_add = verdict_cell(add["d20"])
            o = self._overlay_total(market, f"add-{OVERLAY_ADDS[0]:.0%}")
            verdicts = {v_facto, v_add}
            if verdicts == {"FAIL (retire)"}:
                change = "Stop averaging down into lots <= -8% (suspend the tier in this market)"
                check = (
                    "Shadow-log every first -8% close of a held lot and score the would-be 50% add under "
                    "underwater-d20-v1; lift the suspension when n >= 20 mature and the rule passes"
                )
            elif "PASS (keep)" in verdicts and "FAIL (retire)" not in verdicts:
                change = "Average down mechanically: add 50% at the first close <= -8%, without the support clause"
                check = "Tag the adds; underwater-d20-v1 on them at n >= 5 and again at n >= 20; revert on FAIL"
            else:
                change = "Keep averaging down as the tier states (no mechanical add)"
                check = "underwater-d20-v1 on the tier's own fills, unchanged"
            rows.append(
                [
                    MARKET_NAMES[market],
                    change,
                    f"Real adds into lots <= -8% (any tag): n={s_facto.n} mature, D+20 median {pct(s_facto.median)}, "
                    f"Q1 {pct(s_facto.q1)} → {v_facto}. Systematic 50% add at the first -8% close: n={s_add.n}, "
                    f"median {pct(s_add.median)}, Q1 {pct(s_add.q1)} → {v_add}. Whole window",
                    f"Systematic adds: P&L {pct(o['pnl'])} of average NAV, extra cash {share(o['extra'])} of "
                    f"average NAV, max drawdown {pct(o['base_dd'])} → {pct(o['dd'])}"
                    if o
                    else "n/a",
                    f"{MARKET_NAMES[market]} index {pct(e.index_window[market][0])} over the window, so the adds moved "
                    "with one regime. The tier's own cohort is unscored, and these adds skip its support and thesis clauses",
                    check,
                ]
            )
        # O4: crypto held_majors.
        strict, loose = e.held_majors["strict"], e.held_majors["loose"]
        facto = self._bucket("crypto", "add, lot <= -8%")
        micro = self._bucket("crypto", "add, lot <= -8%", micro=True)
        v_f, s_f = verdict_cell(facto["d20"])
        v_m, s_m = verdict_cell(micro["d20"])
        rows.append(
            [
                "Crypto",
                "Keep `buy.held_majors_support_net` as it stands, and tag its orders in plain ASCII so it can be scored",
                f"The tag never appears in the export. Proxy cohort (held, in profit, limit 3-12% below, filled from "
                f"2026-08-22): strict n={len(strict)}, loose n={len(loose)} → {INSUFFICIENT}. For contrast, adds into "
                f"coins <= -8%: other buys n={s_f.n}, D+20 median {pct(s_f.median)}, Q1 {pct(s_f.q1)} → {v_f}; "
                f"recurring micro-buys n={s_m.n}, median {pct(s_m.median)}, Q1 {pct(s_m.q1)} → {v_m}",
                "Makes the tier measurable; no P&L effect",
                "The export hashes non-ASCII tags, so a Korean label hides the tier",
                "Count tagged fills per month; score the tier's own rule (D+20 median >= 0, Q1 > -8%) at n >= 5",
            ]
        )
        # O5: loss-cut per market. Change only if, at the middle threshold, cutting beat the
        # actual outcome by the last bar in more than half the crossings and on the weighted mean.
        mid = OVERLAY_CUTS[1]
        for market in MARKETS:
            parts, effects = [], []
            for x in OVERLAY_CUTS:
                row = self._loss(market, x, "end")
                d20 = self._loss(market, x, "D+20")
                if row is None:
                    parts.append(f"-{x:.0%}: n=0")
                    continue
                parts.append(
                    f"-{x:.0%}: n={row['n']}{' (' + INSUFFICIENT + ')' if row['n'] < MIN_SAMPLE else ''}, cut beat "
                    f"actual by the last bar in {share(row['cut_better'])} (D+20: "
                    f"{share(d20['cut_better']) if d20 is not None else 'n/a'}), cut minus actual "
                    f"{pct(row['cut_minus_actual_wtd'])} notional-weighted"
                )
                o = self._overlay_total(market, f"cut-{x:.0%}")
                if o:
                    effects.append(
                        f"-{x:.0%}: P&L {pct(o['pnl'])} of avg NAV, drawdown {pct(o['base_dd'])} → "
                        f"{pct(o['dd'])}, cash share {share(o['base_cash_share'])} → {share(o['cash_share'])}"
                    )
            test = self._loss(market, mid, "end")
            supported = (
                test is not None
                and test["n"] >= MIN_SAMPLE
                and test["cut_better"] > 0.5
                and test["cut_minus_actual_wtd"] > 0
            )
            if supported:
                change = f"Cut mechanically at the first close <= -X% (today: {'classification-only' if market != 'crypto' else 'no rule'})"
            else:
                change = (
                    "Keep `sell.loss_cut` classification-only (no mechanical cut)"
                    if market != "crypto"
                    else "Keep crypto without a mechanical cut"
                )
            d20 = self._loss(market, mid, "D+20")
            rows.append(
                [
                    MARKET_NAMES[market],
                    change,
                    "Episodes crossing -X% in the window. " + "; ".join(parts),
                    "; ".join(effects) if effects else "n/a",
                    f"{MARKET_NAMES[market]} index {pct(e.index_window[market][0])} over the window; a cut wins when "
                    f"prices keep falling. At D+20 a -{mid:.0%} cut beat actual in "
                    f"{share(d20['cut_better']) if d20 is not None else 'n/a'}. Cut cash sits idle unless redeployed",
                    "Shadow-log each first close <= -X% of a held lot; at n >= 20 with 60 sessions matured, keep the cut "
                    "only if cut beats actual in more than half and the notional-weighted difference is > 0",
                ]
            )
        # O6: order style. Change only if the unfilled deep intents missed a gain at D+20.
        for market in MARKETS:
            deep, near = (
                self._style(market, "deep_limit"),
                self._style(market, "near_market"),
            )
            if deep is None:
                continue
            small = deep["unfilled_n20"] < MIN_SAMPLE
            if small:
                change = "No change to the buy order style"
            elif deep["unfilled_cost_20"] > 0:
                change = "Buy near the market instead of with deep limits"
            else:
                change = "Keep deep limits for buys"
            o = self._overlay_total(market, "near-market")
            rows.append(
                [
                    MARKET_NAMES[market],
                    change,
                    f"Deep-limit buy intents: {deep['intents']}, fill {share(deep['fill_rate'])}; near-market "
                    f"{near['intents'] if near is not None else 0}, fill {share(near['fill_rate']) if near is not None else 'n/a'}. "
                    f"Unfilled deep intents bought at the next open, D+20: n={deep['unfilled_n20']}, mean "
                    f"{pct(deep['unfilled_cost_20'])}, median {pct(deep['unfilled_cost_median_20'])}. Filled deep buys "
                    f"were {pct(deep['fill_vs_next_open'])} cheaper than the next open"
                    + (f" → {INSUFFICIENT}" if small else ""),
                    (
                        f"Filling every unfilled intent at the next open: P&L {pct(o['pnl'])} of avg NAV, extra cash "
                        f"{share(o['extra'])} of avg NAV, cash share {share(o['base_cash_share'])} → {share(o['cash_share'])}"
                        if o
                        else "n/a"
                    ),
                    f"{MARKET_NAMES[market]} index {pct(e.index_window[market][0])}: in a falling market unfilled limits "
                    "avoid losses, in a rising one they miss gains. Fill-at-open has no slippage; "
                    f"one buy leg costs up to {rate(ROUND_TRIP_COST[market] / 2)}",
                    "Paired per intent: record the next-open price for every buy intent; at n >= 50 matured intents, "
                    "compare intent-level D+20 (unfilled = 0%) between styles; switch only if near-market leads",
                ]
            )
        # O7: parking. Change only if parking lost to idle cash at the observed median hold.
        for market in ("kr", "us"):
            y = e.yields[market]
            if y is None:
                continue
            lots = e.parking_lots[e.parking_lots["market"] == market]
            hold = float(lots["days"].median()) if len(lots) else float("nan")
            be, losing = [], 0
            for alias, g in lots.groupby("alias", sort=True):
                idle = IDLE_CASH_YIELD.get((alias, market), 0.0)
                med = float(g["days"].median())
                if (y["annualized"] - idle) * med / 365.0 - COST_CASES["etf"][
                    market
                ] <= 0:
                    losing += 1
                be.append(
                    f"{self.acct(alias)}: {num(breakeven_days(y['annualized'], idle, COST_CASES['etf'][market]))}"
                    f"-{num(breakeven_days(y['annualized'], idle, COST_CASES['policy'][market]))} days"
                )
            sweep = [
                (alias, v)
                for (alias, m, case), v in e.sweeps.items()
                if m == market and case == "etf"
            ]
            rows.append(
                [
                    MARKET_NAMES[market],
                    "Park only cash expected to stay idle longer than the break-even hold"
                    if losing
                    else "Keep parking as is",
                    f"Instrument total return {pct(y['annualized'])} a year ({pct(y['distribution_return'])} of it over "
                    f"the window from distributions). Break-even hold by account (ETF commission to policy maximum): "
                    f"{'; '.join(be)}. Observed parking lots: n={len(lots)}, median hold {num(hold)} days, "
                    f"{int(lots['realized'].sum())} closed",
                    "Sweeping cash above 10% of NAV, ETF costs, one round trip: "
                    + "; ".join(
                        f"{self.acct(a)} {pct(v['net_once_pct_avg_nav'], 2)} of avg NAV"
                        for a, v in sweep
                    ),
                    "Short holds lose to costs; yields are price plus distributions, pre-tax; the policy cost table is a "
                    "maximum, not a measured fee; KIS cash is taken to earn 0%",
                    "For each parking lot record days held and net return after fees vs the account's cash rate; "
                    "review at 10 closed lots, keep if the median is > 0",
                ]
            )
        # O8: crypto ladders. Change only if far sells fill less and slower than near ones.
        far = e.ladder_rows[e.ladder_rows["distance"] >= 8.0]
        near = e.ladder_rows[
            (e.ladder_rows["distance"] >= 1.0) & (e.ladder_rows["distance"] < 8.0)
        ]
        far_fill, near_fill = (
            (far["filled_share"] > 0).mean(),
            (near["filled_share"] > 0).mean(),
        )
        far_days = far.loc[far["filled_share"] > 0, "days_to_fill"].median()
        near_days = near.loc[near["filled_share"] > 0, "days_to_fill"].median()
        o = self._overlay_total("crypto", "sell-now")
        rows.append(
            [
                "Crypto",
                "Cap resting take-profit sells below +8% above the close, or give them an expiry"
                if far_fill < near_fill and far_days > near_days
                else "Keep the crypto sell ladders as they are",
                f"Sells at +1% to +8%: n={len(near)}, fill {share(near_fill)}, median days to fill {num(near_days)}, "
                f"rest vs selling at the next open {pct(near['rest_vs_sell_now'].mean())}. At +8% or more: "
                f"n={len(far)}, fill {share(far_fill)}, median days to fill {num(far_days)}, rest vs sell now "
                f"{pct(far['rest_vs_sell_now'].mean())}"
                + (f" → {INSUFFICIENT}" if len(far) < MIN_SAMPLE else ""),
                (
                    f"Selling every resting take-profit at the next open instead: P&L {pct(o['pnl'])} of avg NAV, cash share "
                    f"{share(o['base_cash_share'])} → {share(o['cash_share'])}"
                    if o
                    else "n/a"
                ),
                f"Far sells are few; BTC {pct(e.index_window['crypto'][0])} over the window, which favours any sell",
                "Record placement distance, fill time and the price at expiry for each resting sell; at 30 sells per band "
                "compare rest-vs-sell-now by band",
            ]
        )
        # O9: from-scratch rule sets.
        for market in MARKETS:
            for name in OPTION_VARIANTS:
                p = e.rules[(market, name)]
                variant = next(v for v in rulesets.VARIANTS if v.name == name)
                rows.append(
                    [
                        MARKET_NAMES[market],
                        f"From scratch {name}: {variant.describe()}",
                        f"{p['trades']} signals ({p['symbols']} symbols), mean trade {pct(p['mean'])}, median "
                        f"{pct(p['median'])}, wins {share(p['win_rate'])}, worst trade {pct(p['worst'])}. Return per unit "
                        f"{pct(p['return_per_unit'])} vs index {pct(p['index_per_unit'])}. Whole window",
                        f"Return on fully funded capital {pct(p['return_on_capital'])}, max drawdown {pct(p['max_drawdown'])}, "
                        f"capital used on an average day {share(p['avg_deployed_share'])}",
                        "Signals are this window's own picks; one regime; fully funded unit sizing; fills at the open "
                        "without slippage"
                        + (
                            "; averaging down's worst trade is the tail risk"
                            if name.startswith("B")
                            else ""
                        ),
                        "Register the variant before it runs; mock or paper forward on new signals for 60 sessions; keep if "
                        "return per unit beats the index per unit and the max drawdown stays above the limit the operator sets in advance",
                    ]
                )
        return rows

    def section_options(self) -> str:
        headers = [
            "Market", "Rule change", "Evidence (number, n, window)", "Expected effect (in-sample)",
            "Risk / counter-evidence", "Post-adoption check (pre-registered)",
        ]  # fmt: skip
        return "\n".join(
            [
                "## Options table",
                "",
                "Each row is an option, not a recommendation. The rows use thresholds picked by role: -8% (the "
                "tier's floor), -20% (a middle value) and -40% (today's `sell.loss_cut` depth), and from-scratch "
                "variants A2 (tight cut, wide target), A4 (loose cut, wide target) and B3 (capped averaging down "
                "with a stop). The picks were made after the full grids had been computed once, so they are not "
                "blind; every other threshold and variant is in the "
                "full tables below. `Q1` is the lower quartile. P&L effects are on the real book, as a share of "
                "average NAV, cash shares are the equal-weight mean of the market's accounts, and drawdowns are of "
                "account NAV (the worst account of the market).",
                "",
                "Whether a row reads as a change or as keeping today's rule follows a test fixed in the code, not a "
                "row-by-row reading. The tests were written alongside these results, so they are not blind either: "
                "averaging down becomes a stop when both of its cohorts fail the tier's own rule, and a mechanical "
                "add when at least one passes and neither fails; a loss-cut changes only when, at -20%, cutting beat the actual outcome by "
                "the last bar in more than half of at least five crossings and on the notional-weighted mean; the buy "
                "style changes only when the unfilled deep limits missed a gain at D+20 (n >= 5); parking changes "
                "only when an account's median hold lost to idle cash after commission; the crypto ladder changes "
                "only when sells at +8% or more filled less often and more slowly than those at +1% to +8%.",
                "",
                table(headers, self._options_rows()),
            ]
        )

    # ------------------------------------------------------------ Q1
    def section_q1(self) -> str:
        e = self.e
        out = [
            "## 1. Averaging down",
            "",
            "**`buy.underwater_support_net`, scored under `underwater-d20-v1`.** D0 is the order's last fill "
            "date, D+20 the close twenty sessions later on the symbol's own bars, against the order's "
            "execution price. The contract covers fills from 2026-09-08. The tag survives the export in "
            "plain text, so this cohort is exact.",
            "",
        ]
        rows = []
        for market in ("kr", "us"):
            t = self._tier(market)
            s = t["summary"]
            rows.append([
                MARKET_NAMES[market], str(t["accepted"]), str(t["filled"]),
                share(t["filled"] / t["accepted"]) if t["accepted"] else "n/a",
                str(t["contract"]), str(t["mature"]), pct(s.median), pct(s.q1), t["verdict"],
                f"{share(t['eligible_share'])} of {t['lot_known']}",
            ])  # fmt: skip
        out.append(table(
            ["Market", "Tagged buys accepted", "Filled", "Fill rate", "Filled from 2026-09-08",
             "Mature D+20", "D+20 median", "D+20 Q1", "Verdict", "Lot <= -8% at order time"],
            rows,
        ))  # fmt: skip
        filled = e.tier[e.tier["filled"]]
        kr_bars = self.e.retro.book.benchmarks["kr"]
        out += [
            "",
            f"The filled tier orders have D0 from {min(filled['d0']) if len(filled) else 'n/a'} to "
            f"{max(filled['d0']) if len(filled) else 'n/a'}, and the last KR bar is "
            f"{kr_bars.date_at(len(kr_bars) - 1)}, so a fill reaches D+20 only if it is twenty sessions old. "
            "The other tier orders expired, were cancelled, or are `open`, which means no ending was "
            "recorded, not that the order is live. The last column checks clause (2) of the tier at order "
            "time, from the rebuilt average cost and the last complete close.",
            "",
        ]
        if not self.public:
            filled = e.tier[e.tier["filled"]]
            out += [
                table(
                    ["Market", "Account", "Symbol", "D0", "Lot at order time", "D+20"],
                    [[r.market, r.alias, r.symbol, str(r.d0), pct(r.lot_unrealized), pct(r.d20)]
                     for r in filled.itertuples(index=False)],
                ),
                "",
            ]  # fmt: skip
        out += [
            "**Every real buy, by the position's state just before it.** The same D+20 arithmetic, any tag, "
            "the whole window. The position's unrealized P&L is the fill price against its average cost "
            "(`research/strategy_evidence/lots.py`). The verdict column applies the tier's rule to each "
            "bucket for comparison only: these are not tier orders.",
            "",
        ]
        rows = []
        order = [
            "add, lot <= -8%",
            "add, lot -8% to 0%",
            "add, lot >= 0%",
            "new entry (flat before)",
            "add, cost unknown",
        ]
        for market in MARKETS:
            for micro in (False, True) if market == "crypto" else (False,):
                for bucket in order:
                    g = self._bucket(market, bucket, micro=micro)
                    if g.empty:
                        continue
                    s20, s5 = summarize(g["d20"]), summarize(g["d5"])
                    x = (g["d20"] - g["d20_index"]).dropna()
                    rows.append([
                        MARKET_NAMES[market], "recurring micro-buys" if micro else "other buys", bucket,
                        str(len(g)), str(s20.n), pct(s5.median), pct(s20.median), pct(s20.q1), pct(s20.q3),
                        pct(s20.mean), share(s20.positive), pct(x.mean() if len(x) else float("nan")),
                        retire_verdict(s20),
                    ])  # fmt: skip
        out.append(table(
            ["Market", "Scope", "Position before the buy", "Buys", "Mature D+20", "D+5 median", "D+20 median",
             "D+20 Q1", "D+20 Q3", "D+20 mean", "Share > 0", "D+20 mean net of index", "Tier rule, applied"],
            rows,
        ))  # fmt: skip
        rows = []
        for market in ("kr", "us"):
            g = self._bucket(market, "add, lot <= -8%", since=averaging.CONTRACT_START)
            s = summarize(g["d20"])
            rows.append(
                [
                    MARKET_NAMES[market],
                    str(len(g)),
                    str(s.n),
                    pct(s.median),
                    pct(s.q1),
                    retire_verdict(s),
                ]
            )
        out += [
            "",
            "The same `add, lot <= -8%` bucket restricted to fills from 2026-09-08, the contract's start:",
            "",
            table(
                [
                    "Market",
                    "Buys",
                    "Mature D+20",
                    "D+20 median",
                    "D+20 Q1",
                    "Tier rule, applied",
                ],
                rows,
            ),
            "",
            "**A systematic add.** At each episode's first close at or below -X% against average cost, add "
            "50% of the quantity held (the tier's size cap) at that close, once per episode, without the "
            "support and thesis clauses. Scored like the tier; `End` is the return to the last bar.",
            "",
        ]
        rows = []
        for market in MARKETS:
            for x in OVERLAY_ADDS:
                g = self._add_rule(market, x)
                s = summarize(g["d20"])
                rows.append([
                    MARKET_NAMES[market], f"-{x:.0%}", str(len(g)), str(s.n), pct(s.median), pct(s.q1),
                    retire_verdict(s), pct(g["end"].mean() if len(g) else float("nan")),
                    pct(g["end"].median() if len(g) else float("nan")),
                ])  # fmt: skip
        out.append(table(
            ["Market", "First touch", "Adds", "Mature D+20", "D+20 median", "D+20 Q1", "Tier rule, applied",
             "End mean", "End median"],
            rows,
        ))  # fmt: skip
        strict, loose = e.held_majors["strict"], e.held_majors["loose"]
        vs, ss = verdict_cell(strict["d20"])
        vl, sl = verdict_cell(loose["d20"])
        out += [
            "",
            "**`buy.held_majors_support_net` (crypto).** Its tag does not appear anywhere in the export: the "
            "export copies a tag only when it is short plain ASCII, so these orders carry hashed labels. A "
            "proxy stands in for it. It is not proof that a session meant the tier.",
            "",
            table(
                ["Proxy", "Definition", "Filled buys", "Mature D+20", "D+20 median", "D+20 Q1", "Retire rule"],
                [
                    ["strict", "order row, filled from 2026-08-22, coin held and in profit at the order (last complete close vs average cost), limit 3-12% below that close, not a recurring micro-buy",
                     str(len(strict)), str(ss.n), pct(ss.median), pct(ss.q1), vs],
                    ["loose", "the same without the distance clause", str(len(loose)), str(sl.n), pct(sl.median), pct(sl.q1), vl],
                ],
            ),
        ]  # fmt: skip
        return "\n".join(out)

    # ------------------------------------------------------------ Q2
    def section_q2(self) -> str:
        out = [
            "## 2. Loss-cut at the first touch of -X%",
            "",
            "`sell.loss_cut` is classification-only today: it creates no order. For each position episode "
            "(flat back to flat) and threshold, the touch is the first close at or below average cost x "
            "(1 - X). `Hold` is the close at the horizon against the touch close. `Actual` is what became of "
            "the shares held at the touch: later real sells take them first at their real prices, and the "
            "rest is marked at the horizon close. A cut earns 0% less one sell leg ("
            + ", ".join(
                f"{MARKET_NAMES[m]} {rate(ROUND_TRIP_COST[m] / 2)}" for m in MARKETS
            )
            + "), so `Cut better` is the share where the actual outcome was worse than that, and `Cut minus "
            "actual` is the average gain of cutting. Daily bars only: an intraday touch that recovered by the "
            "close is not seen. `Crossed` means the episode was above -X% earlier in the window; "
            "`already below` means its first observed day (an opening lot) was already past -X%.",
            "",
        ]
        rows = []
        s = self.e.losscut
        for market in MARKETS:
            for crossed in (True, False):
                for x in losscut.THRESHOLDS:
                    for horizon, _ in losscut.HORIZONS:
                        row = self._loss(market, x, horizon, crossed)
                        if row is None:
                            continue
                        rows.append([
                            MARKET_NAMES[market], f"-{x:.0%}", "crossed" if crossed else "already below",
                            horizon, str(row["n"]) + (f" ({INSUFFICIENT})" if row["n"] < MIN_SAMPLE else ""),
                            str(row["symbols"]), pct(row["hold_median"]), pct(row["actual_median"]),
                            share(row["cut_better"]), pct(row["cut_minus_actual_mean"]),
                            pct(row["cut_minus_actual_wtd"]), pct(row["worst_after_median"]),
                            share(row["sold_share_mean"]),
                        ])  # fmt: skip
        del s
        out.append(table(
            ["Market", "X", "Group", "Horizon", "Episodes", "Symbols", "Hold median", "Actual median", "Cut better",
             "Cut minus actual, mean", "Cut minus actual, notional-weighted", "Worst close after touch, median",
             "Share of shares really sold by then"],
            rows,
        ))  # fmt: skip
        out += [
            "",
            "Horizons past the last bar are censored, so D+60 counts fewer episodes than D+20. `end` uses "
            "every touch, at different lengths. One episode can count under several thresholds.",
        ]
        return "\n".join(out)

    # ------------------------------------------------------------ Q3
    def section_q3(self) -> str:
        out = [
            "## 3. Buy order style",
            "",
            "ROB-1439 measured fill rates by entry type (KR `deep_limit/proposal` 0.6%, US 2.4%, against "
            "39-89% near the market) and the next-open counterfactual R1. This table adds the intent-level "
            "view: one row per account, symbol and ISO week, styled by its first accepted order. `Actual` "
            "earns from the fill price when filled and 0% when not. `Next open` buys every intent at the "
            "first open after its first order. `Unfilled` is the opportunity cost of the intents that never "
            "filled. Means are equal-weight over intents; no fees on either side.",
            "",
        ]
        rows = []
        for r in self.e.styles.itertuples(index=False):
            rows.append([
                MARKET_NAMES[r.market], r.klass, str(r.intents), str(r.filled), share(r.fill_rate),
                pct(r.fill_vs_next_open, 2), str(r.n5), pct(r.actual_5, 2), pct(r.next_open_5, 2), str(r.n20),
                pct(r.actual_20, 2), pct(r.next_open_20, 2), pct(r.next_open_x20, 2),
                str(r.unfilled_n5), pct(r.unfilled_cost_5, 2),
                str(r.unfilled_n20) + (f" ({INSUFFICIENT})" if r.unfilled_n20 < MIN_SAMPLE else ""),
                pct(r.unfilled_cost_20, 2), pct(r.unfilled_cost_median_20, 2),
            ])  # fmt: skip
        out.append(table(
            ["Market", "Style", "Intents", "Filled", "Fill rate", "Fill vs next open", "n D+5", "Actual D+5",
             "Next open D+5", "n D+20", "Actual D+20", "Next open D+20", "Next open D+20 net of index",
             "Unfilled n D+5", "Unfilled cost D+5", "Unfilled n D+20", "Unfilled cost D+20, mean",
             "Unfilled cost D+20, median"],
            rows,
        ))  # fmt: skip
        return "\n".join(out)

    # ------------------------------------------------------------ Q4
    def section_q4(self) -> str:
        e = self.e
        out = [
            "## 4. Parking",
            "",
            "Instrument total return over the window: price plus distributions (ex-date inside the span, not "
            "reinvested), annualized on calendar days. 357870 and BIL were never held in the window.",
            "",
        ]
        rows = []
        for market in ("kr", "us"):
            y = e.yields[market]
            if y is None:
                continue
            rows.append([
                MARKET_NAMES[market], y["symbol"], f"{y['from']} to {y['to']}",
                str(y["days"]), pct(y["price_return"], 2), pct(y["distribution_return"], 2),
                pct(y["total_return"], 2), pct(y["annualized"], 2),
            ])  # fmt: skip
        out.append(table(
            ["Market", "Instrument", "Span", "Days", "Price return", "Distributions", "Total", "Annualized"], rows
        ))  # fmt: skip
        out += [
            "",
            "Round trip: `policy` is `round_trip_cost_bps` as written (KR "
            f"{rate(COST_CASES['policy']['kr'])}, US {rate(COST_CASES['policy']['us'])}); `etf` is the same "
            f"table's commission alone (KR {rate(COST_CASES['etf']['kr'])}, US {rate(COST_CASES['etf']['us'])}), "
            "since a listed ETF pays no KR sell tax and USD cash needs no FX to buy a USD ETF. Idle cash earns "
            "the account's `cash_yields` rate; KIS has none listed, so 0%. Lots still open are valued at the last "
            "bar as if sold there.",
            "",
        ]
        rows = []
        lots = e.parking_lots
        for (market, alias), g in lots.groupby(["market", "alias"], sort=True):
            if e.yields.get(market) is None:
                continue
            y = e.yields[market]["annualized"]
            idle = IDLE_CASH_YIELD.get((alias, market), 0.0)
            median_hold = float(g["days"].median())
            cells = [
                MARKET_NAMES[market], self.acct(alias), str(len(g)), str(int(g["realized"].sum())),
                num(median_hold), num(float(g["days"].max())), rate(idle), pct(g["total_ret"].mean(), 2),
            ]  # fmt: skip
            for case in ("etf", "policy"):
                be = breakeven_days(y, idle, COST_CASES[case][market])
                net_at_median = (y - idle) * median_hold / 365.0 - COST_CASES[case][
                    market
                ]
                cells += [
                    pct(g[f"net_vs_idle_{case}"].mean(), 2),
                    num(be),
                    "yes" if net_at_median > 0 else "no",
                ]
            rows.append(cells)
        out.append(table(
            ["Market", "Account", "Lots", "Closed", "Median hold, days", "Longest", "Idle cash rate",
             "Lot return, mean", "Net vs idle (etf)", "Break-even days (etf)", "Positive at median hold (etf)",
             "Net vs idle (policy)", "Break-even days (policy)", "Positive at median hold (policy)"],
            rows,
        ))  # fmt: skip
        return "\n".join(out)

    # ------------------------------------------------------------ Q5
    def section_q5(self) -> str:
        e = self.e
        out = [
            "## 5. Crypto sell ladders",
            "",
            "Every accepted crypto sell, by its distance above the last complete UTC daily close when "
            "placed. `Rest vs sell now` compares what the order's coins became by the last bar (filled part at "
            "the fill price then cash, the rest marked at the last close) with selling everything at the next "
            "open. `Rest vs hold` compares it with never selling. Days to fill are UTC days from placement to "
            "the first fill row. `Open at end` means no ending was recorded.",
            "",
        ]
        rows = []
        for r in e.ladders.itertuples(index=False):
            rows.append([
                r.band, str(r.orders) + (f" ({INSUFFICIENT})" if r.orders < MIN_SAMPLE else ""), str(r.filled),
                share(r.fill_rate), num(r.days_to_fill_median, 1), num(r.days_to_fill_max), str(r.open_at_end),
                num(r.open_age_median), pct(r.rest_vs_sell_now, 2), pct(r.rest_vs_sell_now_wtd, 2),
                pct(r.rest_vs_hold, 2), pct(r.hold_vs_sell_now, 2), pct(r.btc_same_span, 2),
            ])  # fmt: skip
        out.append(table(
            ["Distance above close", "Sells", "Filled", "Fill rate", "Days to fill, median", "Days to fill, max",
             "Open at end", "Age of open ones, median days", "Rest vs sell now, mean", "Rest vs sell now, weighted",
             "Rest vs hold, mean", "Hold vs sell now, mean", "BTC, same span"],
            rows,
        ))  # fmt: skip
        still = e.ladder_rows[e.ladder_rows["outcome"] == "open"]
        tp = still.loc[still["take_profit"].astype(bool)]
        out += [
            "",
            f"Still open at the last bar: {len(still)} sells, {len(tp)} of them placed at +1% or more "
            f"(placement distance {num(tp['distance'].min(), 1)}% to {num(tp['distance'].max(), 1)}%, age "
            f"{num(tp['age_at_end'].min())} to {num(tp['age_at_end'].max())} days). Against selling at the next "
            f"open they stand at {pct(tp['rest_vs_sell_now'].mean(), 2)} on average.",
        ]
        return "\n".join(out)

    # ------------------------------------------------------------ Q6
    def section_q6(self) -> str:
        e = self.e
        names = {
            "base": "as traded",
            **{
                f"cut-{x:.0%}": f"loss-cut at first close <= -{x:.0%}"
                for x in OVERLAY_CUTS
            },
            **{
                f"add-{x:.0%}": f"add 50% at first close <= -{x:.0%}"
                for x in OVERLAY_ADDS
            },
            "near-market": "unfilled buy intents filled at the next open",
            "sell-now": "resting take-profits sold at the next open",
        }
        out = [
            "## 6. Idle cash, and how each rule moves it",
            "",
            "The base is ROB-1439's account cash: a lower bound from fills, no deposits or withdrawals, "
            "opening cash the least that keeps cash non-negative. Each rule is replayed as extra trades on top "
            "of the real ones; cut proceeds stay in cash (no redeployment). A buy rule that runs cash below "
            "zero needs extra cash, shown as a share of average NAV and added from the first day. Drawdown is "
            "NAV's largest fall from its running peak, measured with that extra cash in NAV, so a rule that "
            "needs extra cash can show a smaller drawdown than the base.",
            "",
        ]
        rows = []
        for market in MARKETS:
            base = self._overlay(market, "base")
            for name in names:
                frame = self._overlay(market, name)
                if frame is None:
                    continue
                for r in frame.itertuples(index=False):
                    b = base[base["alias"] == r.alias].iloc[0]
                    rows.append([
                        MARKET_NAMES[market], self.acct(r.alias), names[name], share(r.cash_share),
                        pp(r.cash_share - b["cash_share"]), share(r.idle_share), share(r.extra_cash_pct_avg_nav),
                        pct(r.pnl_change_pct_avg_nav, 2), pct(r.max_drawdown),
                    ])  # fmt: skip
        out.append(table(
            ["Market", "Account", "Rule", "Avg cash share", "Change", "Avg cash + parking share",
             "Extra cash needed, % avg NAV", "P&L change, % avg NAV", "Max drawdown"],
            rows,
        ))  # fmt: skip
        out += [
            "",
            "**Parking sweep.** Cash above 10% of NAV held in the market's parking instrument at its measured "
            "yield, less the account's idle cash rate. `Once` pays one round trip on the largest excess, "
            "`monthly` one every 30 days on the average excess.",
            "",
        ]
        rows = []
        for (alias, market, case), v in sorted(e.sweeps.items()):
            rows.append([
                MARKET_NAMES[market], self.acct(alias), case, share(v["excess_share"]), share(v["cash_share_after"]),
                pct(v["gross_pct_avg_nav"], 2), pct(v["net_once_pct_avg_nav"], 2), pct(v["net_monthly_pct_avg_nav"], 2),
            ])  # fmt: skip
        out.append(table(
            ["Market", "Account", "Costs", "Excess cash, % avg NAV", "Cash share after", "Gross, % avg NAV",
             "Net once, % avg NAV", "Net monthly, % avg NAV"],
            rows,
        ))  # fmt: skip
        return "\n".join(out)

    # ------------------------------------------------------------ rule sets
    def section_rulesets(self) -> str:
        e = self.e
        out = [
            "## 7. From-scratch rule sets",
            "",
            "Signals are what the accounts chose to buy: each market, symbol and ISO week with an accepted "
            "buy order or a real buy, parking excluded, once across accounts. Each signal buys one unit of "
            "notional at the next open, so the variants differ only in execution and exit. Every unit pays "
            "the market's full round trip. Portfolio figures treat each unit as fully funded: capital is the "
            "peak number of units held on one day, and drawdown is of capital plus cumulative P&L. The index "
            "column puts the same units in the benchmark over the same dates. The variant list is fixed in "
            "`rulesets.py` and every variant is shown.",
            "",
        ]
        counts = e.signals.groupby("market").size()
        out.append(
            "Signals: "
            + ", ".join(f"{MARKET_NAMES[m]} {int(counts.get(m, 0))}" for m in MARKETS)
            + "."
        )
        out.append("")
        rows = []
        for market in MARKETS:
            for v in rulesets.VARIANTS:
                p = e.rules[(market, v.name)]
                if not p.get("trades"):
                    continue
                ci = p["ci"]
                rows.append([
                    MARKET_NAMES[market], v.name, v.describe(), str(p["trades"]), str(p["symbols"]), num(p["units"]),
                    pct(p["mean"]), pct(p["median"]), share(p["win_rate"]), pct(p["worst"]),
                    f"{pct(ci[0])} to {pct(ci[1])}" if ci else "n/a", pct(p["return_per_unit"]),
                    pct(p["index_per_unit"]), pct(p["return_on_capital"]), pct(p["max_drawdown"]),
                    share(p["avg_deployed_share"]),
                    ", ".join(f"{k} {c}" for k, c in p["reasons"].items()),
                ])  # fmt: skip
        out.append(table(
            ["Market", "Variant", "Rule", "Signals", "Symbols", "Units", "Mean trade", "Median trade", "Wins",
             "Worst trade", "Mean trade 95% CI (symbols)", "Return per unit", "Index per unit",
             "Return on capital", "Max drawdown", "Capital used, avg day", "Exits"],
            rows,
        ))  # fmt: skip
        out += [
            "",
            "For scale, the real lots bought in the window returned, from ROB-1439's headline: "
            + "; ".join(
                f"{MARKET_NAMES[m]} {pct(self._retro_return(m), 2)}" for m in MARKETS
            )
            + ". Those are cost-weighted and before fees, so they are context, not a like-for-like row.",
        ]
        return "\n".join(out)

    def _retro_return(self, market: str) -> float:
        m = self.e.retro.matches
        lots = m[(m["market"] == market) & (m["origin"] == "fill")]
        lots = lots[[not is_parking(market, sym) for sym in lots["symbol"]]]
        return float(lots["pnl"].sum() / lots["base_cost"].sum())

    # ------------------------------------------------------------ limits
    def section_limits(self) -> str:
        return "\n".join(
            [
                "## Method and limits",
                "",
                "- **In-sample, one window.** 2026-05-01 to 2026-11-01. KR fell over the whole window, US rose, "
                "crypto fell. Each counterfactual is computed on the data it would be judged on and overlaps the "
                "others, so they do not add up.",
                "- **Not an experiment look.** Like ROB-1439, this is a descriptive replay of live trading. It reads "
                "no sealed data and is not registered in `research/experiments`. A rule chosen from it needs its own "
                "pre-registered forward test (the last column of the options table).",
                "- **Average cost** is rebuilt from fills (`lots.py`): a buy moves it to the weighted mean, a sell "
                "leaves it, a seed row resets quantity and cost to the broker's figures, and days with older "
                "holdings of unknown cost are left out. It is not read from the broker.",
                "- **Daily bars.** Touches, cuts and exits use closes; entries use the next open. No intraday low, "
                "no slippage, no price impact.",
                "- **Costs** come from the policy's `round_trip_cost_bps`, a conservative maximum, not measured fees. "
                "Parking also shows a commission-only case. Taxes beyond those rates, FX on KRW/USD and dividends on "
                "stocks are left out.",
                "- **Cash** is the ROB-1439 lower bound: no balances or external flows are in the export, so cash "
                "shares are estimates and the extra cash a buy rule needs is relative to that bound.",
                "- **Tags.** Only short plain-ASCII strategy tags survive the export. `underwater_support_net` does, "
                "`held_majors_support_net` does not, so the crypto tier is a proxy.",
                "- **Rule-set signals** are the accounts' own picks in this window, so they carry its selection. "
                "Units are equal notional and fully funded; the real accounts sized differently.",
                f"- **{INSUFFICIENT}** marks any cohort below {MIN_SAMPLE} observations, the tiers' own minimum.",
                "- **Export v1.** No question here needed the v2 fields (`fill_seq`, seed flag, daily balances). "
                "Daily balances would replace the cash lower bound in question 6.",
            ]
        )

    def render(self) -> str:
        parts = [
            self.section_header(),
            self.section_options(),
            self.section_q1(),
            self.section_q2(),
            self.section_q3(),
            self.section_q4(),
            self.section_q5(),
            self.section_q6(),
            self.section_rulesets(),
            self.section_limits(),
        ]
        return "\n\n".join(parts) + "\n"
