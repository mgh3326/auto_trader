"""Render the results twice from the same numbers.

The repository is public and the export is private trading data, so the committed
report is ``public=True``: ratios and counts, masked symbols, masked accounts and
no amounts. The full version with symbols and amounts stays outside the repository.
Every number in either report is computed here. No figure is typed by hand.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from research.trade_retro.canonical import IMPLAUSIBLE_DEVIATION, PARKING
from research.trade_retro.entry import DEEP_PCT, fill_rates
from research.trade_retro.markouts import HORIZONS, cluster_ci
from research.trade_retro.prices import BENCHMARK_NAMES, PriceBook
from research.trade_retro.rules import EXIT_HORIZON, weighted

MARKETS = (("kr", "KR", "KRW"), ("us", "US", "USD"), ("crypto", "Crypto", "KRW"))
DUST = {"KRW": 1000.0, "USD": 1.0}
RUN_COMMAND = (
    "uv run python -m research.trade_retro.run report "
    "--export ~/work/herdr-artifacts/dots-history-2026-11-01 "
    "--prices ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 "
    "--public-out docs/research/rob-1439-trade-retrospective-2026-11-01.md "
    "--private-dir ~/work/herdr-artifacts/rob-1439-trade-retro"
)


@dataclass
class Results:
    manifest: dict
    manifest_sha256: str
    book: PriceBook
    diagnostics: dict
    orders: pd.DataFrame
    fills: pd.DataFrame
    matches: pd.DataFrame
    positions: pd.DataFrame
    unknown_sells: pd.DataFrame
    executions: pd.DataFrame
    idle: dict[tuple[str, str], dict]
    idle_meta: dict[tuple[str, str], dict]
    r1: pd.DataFrame
    r2: pd.DataFrame
    r3: pd.DataFrame
    r4: pd.DataFrame
    embedded_buys: int
    split_adjusted: list[tuple]
    implausible: list[dict]
    unpriced_open: list[tuple]
    audit: dict
    scenarios: list
    cutover: datetime


def account_labels(aliases: list[str], salt: str) -> dict[str, str]:
    """Neutral labels, ordered by a hash with a value that is not in the report.

    Without the salt, anyone could rebuild the order from the aliases in the source.
    """
    ordered = sorted(
        aliases, key=lambda a: hashlib.sha256(f"{salt}|{a}".encode()).hexdigest()
    )
    return {alias: f"Account {chr(ord('A') + i)}" for i, alias in enumerate(ordered)}


def pct(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:+.{digits}f}%"


def share(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:.1f}%"


def money(value, currency: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    digits = 2 if currency == "USD" else 0
    return f"{value:+,.{digits}f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_No rows._\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


class Renderer:
    def __init__(self, results: Results, public: bool) -> None:
        self.r = results
        self.public = public
        aliases = sorted(set(results.fills["alias"]) | set(results.orders["alias"]))
        self.labels = account_labels(
            aliases, results.manifest.get("salt_fingerprint", "")
        )

    def acct(self, alias: str) -> str:
        return self.labels[alias] if self.public else alias

    # ------------------------------------------------------------- section 2
    def _position_rows(
        self, frame: pd.DataFrame, label: str, currency: str
    ) -> list[str]:
        cost = frame["base_cost"].sum()
        pnl = frame["pnl"].sum()
        real_cost = frame["realized_cost"].sum()
        open_cost = frame["unrealized_cost"].sum()
        total = pnl / cost if cost else float("nan")
        index_ret = frame["index_pnl"].sum() / cost if cost else float("nan")
        row = [
            label,
            len(frame),
            int(frame["is_open"].sum()),
            pct(frame["realized_pnl"].sum() / real_cost) if real_cost else "n/a",
            pct(frame["unrealized_pnl"].sum() / open_cost) if open_cost else "n/a",
            pct(total),
            pct(float(frame["ret"].median())),
            share(float((frame["pnl"] > 0).mean())),
            f"{np.average(frame['days'], weights=frame['base_cost']):.0f}",
            f"{frame['days'].median():.0f}",
            pct(total),
            pct(index_ret),
            pct(total - index_ret),
        ]
        if not self.public:
            row += [
                money(cost, currency),
                money(pnl, currency),
                money(pnl - frame["index_pnl"].sum(), currency),
            ]
        return row

    def section_positions(self, market: str, currency: str) -> str:
        r = self.r
        all_pos = r.positions[r.positions["market"] == market]
        all_pos = all_pos[all_pos["base_cost"] >= DUST[currency]]
        parking = np.array(
            [(market, s) in PARKING for s in all_pos["symbol"]], dtype=bool
        )
        core, park = all_pos[~parking], all_pos[parking]
        headers = [
            "Scope",
            "Positions",
            "Still open",
            "Realized",
            "Unrealized",
            "Total",
            "Median position",
            "Winners",
            "Hold days (cost-wtd)",
            "Hold days (median)",
            "vs cash (0%)",
            "Index, same dates",
            "vs index",
        ]
        if not self.public:
            headers += ["Cost basis", "P&L", "P&L minus index P&L"]
        rows: list[list[str]] = []
        origin_names = (
            ("fill", "bought in window"),
            ("seed", "opening lots, from the seed-date close"),
        )
        for alias in sorted(core["alias"].unique(), key=self.acct):
            for origin, name in origin_names:
                part = core[(core["alias"] == alias) & (core["origin"] == origin)]
                if len(part):
                    rows.append(
                        self._position_rows(
                            part, f"{self.acct(alias)}: {name}", currency
                        )
                    )
        for origin, name in origin_names:
            part = core[core["origin"] == origin]
            if len(part):
                rows.append(self._position_rows(part, f"**All: {name}**", currency))
        if len(park):
            rows.append(
                self._position_rows(park, "Parking instruments (cash-like)", currency)
            )

        out = [table(headers, rows)]
        seeds = core[core["origin"] == "seed"]
        if len(seeds):
            out.append(
                f"\nOpening lots measured on the broker's average cost instead: "
                f"{pct(seeds['pnl_on_cost'].sum() / seeds['cost'].sum())} "
                f"(n={len(seeds)}). That cost predates the window, so no index comparison "
                f"applies to it.\n"
            )
        unknown = r.unknown_sells[r.unknown_sells["market"] == market]
        sold = r.fills[
            (r.fills["market"] == market)
            & (r.fills["side"] == "sell")
            & ~r.fills["seed"]
        ]
        sold_notional = float((sold["qty"] * sold["price"]).sum())
        out.append("\nSells with no known basis, excluded from every return above:\n\n")
        unknown_rows = []
        for alias in sorted(sold["alias"].unique(), key=self.acct):
            part = unknown[unknown["alias"] == alias] if len(unknown) else unknown
            part_notional = (
                float((part["qty"] * part["price"]).sum()) if len(part) else 0.0
            )
            alias_sold = sold[sold["alias"] == alias]
            alias_notional = float((alias_sold["qty"] * alias_sold["price"]).sum())
            row = [
                self.acct(alias),
                len(part),
                share(
                    part_notional / alias_notional if alias_notional else float("nan")
                ),
            ]
            if not self.public:
                row.append(money(part_notional, currency))
            unknown_rows.append(row)
        total_unknown = (
            float((unknown["qty"] * unknown["price"]).sum()) if len(unknown) else 0.0
        )
        row = [
            "**All**",
            len(unknown),
            share(total_unknown / sold_notional if sold_notional else float("nan")),
        ]
        if not self.public:
            row.append(money(total_unknown, currency))
        unknown_rows.append(row)
        heads = ["Scope", "Sell pieces", "Share of sold notional"]
        if not self.public:
            heads.append("Proceeds")
        out.append(table(heads, unknown_rows))
        return "".join(out)

    # ------------------------------------------------------------- section 3
    def section_entry(self, market: str) -> str:
        r = self.r
        orders = r.orders[(r.orders["market"] == market) & ~r.orders["parking"]]
        out: list[str] = []
        accepted = orders[orders["is_accepted"]]
        out.append(
            f"Accepted orders: {len(accepted)} from {len(orders)} rows, excluding "
            f"parking instruments.\n\n"
        )
        heads = [
            "Side",
            "Entry type",
            "Accepted",
            "Filled",
            "Partial",
            "Expired",
            "Cancelled",
            "Open",
            "Fill rate",
            "Fill rate, open removed",
            "Intents",
            "Intent fill rate",
        ]

        def rate_rows(frame: pd.DataFrame, first: str, label) -> list[list[str]]:
            return [
                [
                    row["side"],
                    label(row[first]),
                    row["accepted"],
                    row["filled"],
                    row["partial"],
                    row["expired"],
                    row["cancelled"],
                    row["open"],
                    share(row["fill_rate"]),
                    share(row["fill_rate_closed"]),
                    row["intents"],
                    share(row["intent_fill_rate"]),
                ]
                for row in frame.to_dict("records")
            ]

        if len(accepted):
            frame = fill_rates(orders, ["side", "entry_type"])
            out.append(table(heads, rate_rows(frame, "entry_type", str)))
            out.append(
                f"\nLimit distance from the reference close (policy band edge {DEEP_PCT:.0f}%):\n\n"
            )
            frame = fill_rates(orders, ["side", "band"])
            out.append(
                table(["Side", "Distance", *heads[2:]], rate_rows(frame, "band", str))
            )
            out.append("\nBy account:\n\n")
            frame = fill_rates(orders, ["side", "alias"])
            frame = frame.sort_values(
                ["side", "alias"],
                key=lambda s: s.map(self.acct) if s.name == "alias" else s,
            )
            out.append(
                table(
                    ["Side", "Account", *heads[2:]],
                    rate_rows(frame, "alias", self.acct),
                )
            )

        executions = r.executions[
            (r.executions["market"] == market) & ~r.executions["parking"]
        ]
        out.append(
            "\nMarkouts of executed orders, mean with the number of orders in brackets. "
            "A positive value means the trade looked right. `x` is the same value net of "
            "the index. The 95% interval resamples symbols.\n\n"
        )
        cols = ["Side", "Entry type", "Orders", "Symbols"]
        for h in HORIZONS:
            cols += [f"{h}s", f"{h}s x"]
        cols.append("20s 95% CI")
        rows = []
        for (side, kind), group in executions.groupby(
            ["side", "entry_type"], sort=True
        ):
            row: list = [side, kind, len(group), group["symbol"].nunique()]
            for h in HORIZONS:
                m, x = (
                    group[f"m{h}"].dropna().astype(float),
                    group[f"x{h}"].dropna().astype(float),
                )
                row += [
                    f"{pct(float(m.mean()) if len(m) else float('nan'))} ({len(m)})",
                    pct(float(x.mean()) if len(x) else float("nan")),
                ]
            valid = group.dropna(subset=["m20"])
            ci = (
                cluster_ci(
                    valid["m20"].astype(float).to_numpy(), valid["symbol"].to_numpy()
                )
                if len(valid)
                else None
            )
            row.append(f"{pct(ci[0])} to {pct(ci[1])}" if ci else "n/a")
            rows.append(row)
        out.append(table(cols, rows))
        return "".join(out)

    # ------------------------------------------------------------- section 4
    def section_idle(self, market: str, currency: str) -> str:
        r = self.r
        heads = [
            "Account",
            "Days",
            "From",
            "To",
            "Avg cash share",
            "Avg cash + parking share",
            "Last cash share",
            "Last cash + parking share",
            "Forgone, % of avg cash",
            "Forgone, % of avg NAV",
            "Index return, same dates",
            "Account return, same dates",
        ]
        if not self.public:
            heads += [
                "Opening cash (lower bound)",
                "Avg cash",
                "Avg NAV",
                "Forgone",
                "Inferred opening holdings",
            ]
        rows = []
        items = [(k, v) for k, v in r.idle.items() if k[1] == market]
        for (alias, _m), s in sorted(items, key=lambda kv: self.acct(kv[0][0])):
            meta = r.idle_meta[(alias, market)]
            row = [
                self.acct(alias),
                s["days"],
                s["start"],
                s["end"],
                share(s["cash_share"]),
                share(s["idle_share"]),
                share(s["final_cash_share"]),
                share(s["final_idle_share"]),
                pct(s["forgone_pct_of_avg_cash"]),
                pct(s["forgone_pct_of_avg_nav"]),
                pct(s["index_return"]),
                pct(s["nav_return"]),
            ]
            if not self.public:
                row += [
                    money(meta["opening_cash_lower_bound"], currency),
                    money(s["avg_cash"], currency),
                    money(s["avg_nav"], currency),
                    money(s["forgone_cash"], currency),
                    money(meta["inferred_opening_value"], currency),
                ]
            rows.append(row)
        return table(heads, rows)

    # ------------------------------------------------------------- section 5
    def _context(self, key: str | None) -> tuple[str, str, str]:
        executions = self.r.executions
        if key is None:
            return "seed", "", ""
        row = executions[executions["key"] == key]
        if row.empty:
            return "unknown", "", ""
        first = row.iloc[0]
        tag = first["proposal_tag"] or first["tag"] or ""
        rung = first["rung_index"]
        has_rung = rung is not None and rung == rung
        return (
            first["entry_type"],
            (str(int(rung)) if has_rung else ""),
            _short_tag(tag),
        )

    def section_winners(self, market: str, currency: str) -> str:
        r = self.r
        positions = r.positions[r.positions["market"] == market]
        positions = positions[
            np.array(
                [(market, s) not in PARKING for s in positions["symbol"]], dtype=bool
            )
        ]
        groups = (
            positions.assign(day_cost=positions["days"] * positions["base_cost"])
            .groupby(["alias", "symbol", "episode"], sort=True)
            .agg(
                pnl=("pnl", "sum"),
                base_cost=("base_cost", "sum"),
                index_pnl=("index_pnl", "sum"),
                day_cost=("day_cost", "sum"),
                is_open=("is_open", "max"),
                origins=("origin", lambda s: "+".join(sorted(set(s)))),
                opened=("opened", "min"),
                closed=("closed", "max"),
            )
            .reset_index()
        )
        groups = groups[groups["base_cost"] >= DUST[currency]]
        gross = float(groups["pnl"].abs().sum())
        heads = [
            "Rank",
            "Account",
            "Origin",
            "Return",
            "vs index",
            "Share of gross absolute P&L",
            "Hold days",
            "State",
            "Entry type",
            "Entry rung",
            "Entry tag",
            "Exit type",
            "Exit rung",
            "Exit tag",
        ]
        if not self.public:
            heads = ["Symbol", *heads, "Opened", "Last", "Cost basis", "P&L"]
        out = [f"Positions ranked: {len(groups)}, parking and dust excluded.\n\n"]
        matches = r.matches[r.matches["market"] == market]
        for title, prefix, picked in (
            ("Largest winners", "W", groups[groups["pnl"] > 0].nlargest(5, "pnl")),
            ("Largest losers", "L", groups[groups["pnl"] < 0].nsmallest(5, "pnl")),
        ):
            rows = []
            for rank, row in enumerate(picked.to_dict("records"), start=1):
                part = matches[
                    (matches["alias"] == row["alias"])
                    & (matches["symbol"] == row["symbol"])
                    & (matches["episode"] == row["episode"])
                ]
                entry = (
                    part.groupby("entry_key", dropna=False)["base_cost"].sum().idxmax()
                )
                entry = None if entry is None or entry != entry else entry
                exits = part[part["exit_key"].notna()]
                exit_key = (
                    exits.groupby("exit_key")["base_cost"].sum().idxmax()
                    if len(exits)
                    else None
                )
                e_type, e_rung, e_tag = self._context(entry)
                x_type, x_rung, x_tag = (
                    self._context(exit_key) if exit_key else ("held", "", "")
                )
                cost = row["base_cost"]
                line = [
                    f"{prefix}{rank}",
                    self.acct(row["alias"]),
                    row["origins"],
                    pct(row["pnl"] / cost),
                    pct((row["pnl"] - row["index_pnl"]) / cost),
                    share(abs(row["pnl"]) / gross if gross else float("nan")),
                    f"{row['day_cost'] / cost:.0f}",
                    "open" if row["is_open"] else "closed",
                    e_type,
                    e_rung,
                    e_tag,
                    x_type,
                    x_rung,
                    x_tag,
                ]
                if not self.public:
                    line = [
                        row["symbol"],
                        *line,
                        row["opened"],
                        row["closed"],
                        money(cost, currency),
                        money(row["pnl"], currency),
                    ]
                rows.append(line)
            out.append(f"**{title}**\n\n")
            out.append(table(heads, rows))
            out.append("\n")
        return "".join(out)

    def section_rules(self, market: str, currency: str) -> str:
        r = self.r
        rows: list[list[str]] = []
        amount = not self.public

        def add(name: str, n: str, effect: str, value: float | None = None) -> None:
            line = [name, n, effect]
            if amount:
                line.append(money(value, currency) if value is not None else "")
            rows.append(line)

        r1 = r.r1[r.r1["market"] == market] if len(r.r1) else r.r1
        for klass, group in r1.groupby("klass", sort=True) if len(r1) else ():
            g20 = group.dropna(subset=["r20"])
            g5 = group.dropna(subset=["r5"])
            add(
                f"R1 Buy the unfilled `{klass}` orders at the next open, hold 20 sessions",
                f"{len(g20)} of {len(group)} reach 20s ({len(g5)} reach 5s)",
                f"20s {pct(weighted(g20['r20'], g20['notional']))}, net of index "
                f"{pct(weighted(g20['x20'], g20['notional']))}; 5s "
                f"{pct(weighted(g5['r5'], g5['notional']))}, net of index "
                f"{pct(weighted(g5['x5'], g5['notional']))}",
                float((g20["r20"].astype(float) * g20["notional"]).sum()),
            )
        r2 = r.r2[r.r2["market"] == market] if len(r.r2) else r.r2
        for family, group in r2.groupby("family", sort=True) if len(r2) else ():
            g20 = group.dropna(subset=["m20"])
            g5 = group.dropna(subset=["m5"])
            gain20 = -g20["m20"].astype(float) * g20["notional"]
            add(
                f"R2 Hold instead of selling: `{family}`",
                f"{len(g20)} of {len(group)} reach 20s ({len(g5)} reach 5s)",
                f"20s {pct(gain20.sum() / g20['notional'].sum() if len(g20) else float('nan'))} of "
                f"sold notional; 5s "
                f"{pct((-g5['m5'].astype(float) * g5['notional']).sum() / g5['notional'].sum() if len(g5) else float('nan'))}",
                float(gain20.sum()),
            )
        r3 = r.r3[r.r3["market"] == market] if len(r.r3) else r.r3
        if len(r3):
            delta = float((r3["rule_pnl"] - r3["actual_pnl"]).sum())
            add(
                f"R3 Exit every in-window lot {EXIT_HORIZON} sessions after entry",
                f"{r3['lot'].nunique()} lots, "
                f"{int(r3.drop_duplicates('lot')['reached'].sum())} reach the horizon",
                f"actual {pct(r3['actual_pnl'].sum() / r3['cost'].sum())} of cost, rule "
                f"{pct(r3['rule_pnl'].sum() / r3['cost'].sum())}",
                delta,
            )
        r4 = r.r4[r.r4["market"] == market] if len(r.r4) else r.r4
        for kind, group in r4.groupby("entry_type", sort=True) if len(r4) else ():
            add(
                f"R4 Never open lots of type `{kind}`",
                f"{group['lot'].nunique()} lots",
                f"removes a return of {pct(group['pnl'].sum() / group['base_cost'].sum())} on "
                f"{share(group['base_cost'].sum() / r4['base_cost'].sum())} of in-window cost; net of "
                f"index {pct((group['pnl'].sum() - group['index_pnl'].sum()) / group['base_cost'].sum())}",
                -float(group["pnl"].sum()),
            )
        if len(r4):
            delta = float(r4["index_pnl"].sum() - r4["pnl"].sum())
            add(
                "R5 Buy the index instead, for each in-window lot's money and dates",
                f"{r4['lot'].nunique()} lots",
                f"actual {pct(r4['pnl'].sum() / r4['base_cost'].sum())}, index "
                f"{pct(r4['index_pnl'].sum() / r4['base_cost'].sum())}",
                delta,
            )
        for (alias, m), s in sorted(r.idle.items(), key=lambda kv: self.acct(kv[0][0])):
            if m == market:
                add(
                    f"R6 Hold the lower-bound idle cash in the index: {self.acct(alias)}",
                    f"{s['days']} days",
                    f"{pct(s['forgone_pct_of_avg_nav'])} of average NAV",
                    s["forgone_cash"],
                )
        heads = ["Candidate rule", "Sample", "Counterfactual"]
        if amount:
            heads.append(f"Change in {currency}")
        return table(heads, rows)

    # ------------------------------------------------------------- limits
    def section_limits(self) -> str:
        r = self.r
        d = r.diagnostics
        raw = d["outcome_counts_raw"]
        fixed = d["outcome_counts_corrected"]
        out_rows = [
            [name, raw.get(name, 0), fixed.get(name, 0)]
            for name in (
                "filled",
                "partial",
                "expired",
                "cancelled",
                "rejected",
                "open",
                "replaced",
                "not_submitted",
                "anomaly",
                "unknown",
            )
        ]
        hashed = int(
            sum(
                r.orders["tag"].str.startswith("oth_")
                | r.orders["proposal_tag"].str.startswith("oth_")
            )
        )
        with_tag = int(
            ((r.orders["tag"] != "") | r.orders["proposal_tag"].ne("")).sum()
        )
        window = r.manifest["window"]
        linked = r.orders.loc[r.orders["source"] == "proposal", "created"]
        first_proposal = linked.min().date() if len(linked) else "no date"
        bullets = [
            f"**Six months only.** The export window is {window['since_utc'][:10]} to "
            f"{window['until_utc'][:10]}, one market regime, with no out-of-sample period.",
            f"**Order records start late.** The first order row is "
            f"{min(d['first_order_by_alias'].values())}, so earlier fills count as `external` "
            f"with no limit price. Proposal links start at {first_proposal}, so "
            f"`session` against `proposal` is also an early-against-late comparison.",
            f"**Hashed tags.** {hashed} of {with_tag} tagged orders carry an `oth_*` hash because "
            f"Korean strategy names were not exported. They group, but their meaning is gone.",
            f"**Stale order rows.** {d['open_rows_with_ledger_fills']} order rows say `open` while "
            f"the execution ledger holds their fills, and {d['order_ledger_only_fills']} fills "
            f"exist only on an order row. The analysis takes the larger quantity from either "
            f"source. A row still `open` has no recorded ending; that does not prove it is live.",
            "**Repeated fills.** See Data checks. Fills with no order row can only be tested against "
            "inventory, so a repeat that inventory does not expose would stay in.",
            f"**Fill times.** Reconciler rows can carry the order's time, so fill latency is not "
            f"measured. Fills found only on an order row use the order's date. "
            f"{d['kis_day_fallback_fills']} fills stamped a day away from their order take the "
            f"order's day. Other fills from the same broker can also be a day early, which shifts "
            f"their markouts but not their matching.",
            f"**Opening lots.** {d['seed_rows']} seed rows carry a broker average cost from an "
            f"unknown date, and some are plugs added months later for holdings the ledger could "
            f"not explain. Holding days on them are lower bounds. {r.embedded_buys} buys before "
            f"the {r.cutover.date()} cutover are already inside a seed and make no lot.",
            f"**Fills that cannot be trades.** {len(r.implausible)} ledger fills sit more than "
            f"{IMPLAUSIBLE_DEVIATION:.0%} from their session's close, at prices like an old average "
            f"cost. Their quantity reconciles with later holdings, so they count as opening lots "
            f"and make no markout or cash flow. They are worth a ledger check.",
            "**Unknown-basis sells.** One account has no seed rows, so sells of holdings older "
            "than its ledger have no cost and earn no return. That account's realized figures "
            "cover only what it bought in the window. Section 2 shows each account's share.",
            "**No balances in the export.** Idle cash is a lower bound derived from fills, and "
            "deposits, withdrawals, FX conversions and holdings never traded in the window are "
            "invisible. It is not the current cash balance. Compare the forgone rate, not the "
            "share, against what the operator knows the account holds.",
            "**Gross returns.** No fees, taxes, FX or dividends. Stock, index and parking "
            "instruments are all price return, which understates dividend payers and "
            "especially parking instruments, whose yield is mostly distributions.",
            "**One currency per market.** US figures are in USD and exclude the KRW/USD move. "
            "No total across markets is given.",
            "**Reference price.** Limit distance uses the last complete daily close, not the live "
            "quote the policy band uses, so a class can differ from what the session saw.",
            "**Markouts are close-based.** Session 0 is the fill's session. A fill after the "
            "close counts from that same session. A crypto session is one UTC day. Horizons past "
            "the last bar are censored, not zero, so 20-session samples are smaller and older.",
            "**Counterfactuals are in-sample.** They ignore price impact, fees and available "
            "cash. R1 assumes a fill at the open that the real limit order did not get. They "
            "overlap, so they do not add up. They are candidates to test, not findings.",
            "**Not an experiment look.** This is a descriptive retrospective of live trading, "
            "not a test-set read under `research/experiments`, and it uses no sealed data.",
            "**Mock and paper excluded.** Only real-money tables are read. Kiwoom mock, NHPLUG "
            "mock and Binance demo are outside the export.",
        ]
        missing = sorted(r.book.missing)
        sensitivity = []
        for market, name, _currency in MARKETS:
            lots = r.r4[r.r4["market"] == market] if len(r.r4) else r.r4
            if len(lots):
                cost = lots["base_cost"].sum()
                sensitivity.append(
                    f"{name} {pct(lots['index_pnl'].sum() / cost)} from the close, "
                    f"{pct(lots['index_pnl_open'].sum() / cost)} from the open"
                )
        bullets.append(
            "**Index start.** A lot's stock leg starts at its fill price and its index leg at "
            "that day's close. The fill's time of day is not reliable, so neither end of the day "
            "is exact. The index return on lots bought in the window: "
            + "; ".join(sensitivity)
            + "."
        )
        if self.public:
            bullets.append(
                f"**Price coverage.** Symbols with bars: {len(r.book.bars)}. Without: "
                f"{len(missing)}. Their orders have no distance or markout and appear as "
                f"`no_reference` or `n/a`. Open positions with no price to mark: "
                f"{len(r.unpriced_open)}, left out of the returns."
            )
            bullets.append(
                f"**Splits.** Symbols with a split after a fill: {len(r.split_adjusted)}, "
                f"converted to split-adjusted units."
            )
        else:
            bullets.append(
                f"**Price coverage.** Symbols with bars: {len(r.book.bars)}. Missing: "
                f"{', '.join(f'{m}/{s}' for m, s in missing) or 'none'}. Open positions with no "
                f"price to mark, left out of the returns: {r.unpriced_open or 'none'}."
            )
            bullets.append(f"**Splits.** Converted: {r.split_adjusted or 'none'}.")
            bullets.append(
                "**Fills treated as opening lots.** "
                + ", ".join(
                    f"{i['alias']} {i['symbol']} {i['side']} {i['day']} ({i['deviation']:+.1%})"
                    for i in r.implausible
                )
                + "."
            )
        lines = "\n".join(f"- {b}" for b in bullets)
        return (
            f"{lines}\n\n`terminal_outcome` across the live order tables, as exported and after "
            f"correcting from fills:\n\n"
            + table(["Value", "As exported", "After correction"], out_rows)
        )

    # ------------------------------------------------------------- headline
    def section_headline(self) -> str:
        r = self.r
        rows = []
        for market, name, _currency in MARKETS:
            lots = r.r4[r.r4["market"] == market] if len(r.r4) else r.r4
            orders = r.orders[(r.orders["market"] == market) & ~r.orders["parking"]]
            buys = orders[orders["is_accepted"] & (orders["side"] == "buy")]
            sells = r.executions[
                (r.executions["market"] == market)
                & (r.executions["side"] == "sell")
                & ~r.executions["parking"]
            ]
            buy_exec = r.executions[
                (r.executions["market"] == market)
                & (r.executions["side"] == "buy")
                & ~r.executions["parking"]
            ]
            buy_large = buy_exec[buy_exec["source"] != "external_micro"]
            cost = lots["base_cost"].sum() if len(lots) else 0.0
            idle = [
                s
                for (_alias, m), s in sorted(
                    r.idle.items(), key=lambda kv: self.acct(kv[0][0])
                )
                if m == market
            ]
            rows.append(
                [
                    name,
                    lots["lot"].nunique() if len(lots) else 0,
                    pct(lots["pnl"].sum() / cost) if cost else "n/a",
                    pct(lots["index_pnl"].sum() / cost) if cost else "n/a",
                    pct((lots["pnl"].sum() - lots["index_pnl"].sum()) / cost)
                    if cost
                    else "n/a",
                    share(float((buys["filled_qty"] > 1e-9).mean()))
                    if len(buys)
                    else "n/a",
                    pct(float(buy_exec["m20"].dropna().astype(float).mean()))
                    if buy_exec["m20"].notna().any()
                    else "n/a",
                    pct(float(buy_large["m20"].dropna().astype(float).mean()))
                    if buy_large["m20"].notna().any()
                    else "n/a",
                    pct(float(sells["m20"].dropna().astype(float).mean()))
                    if sells["m20"].notna().any()
                    else "n/a",
                    " / ".join(share(s["cash_share"]) for s in idle),
                    " / ".join(pct(s["index_return"]) for s in idle),
                ]
            )
        return table(
            [
                "Market",
                "Lots bought in window",
                "Return (realized + marked)",
                "Index, same dates",
                "vs index",
                "Buy fill rate",
                "Buy markout 20s",
                "Buy markout 20s, without recurring micro-buys",
                "Sell markout 20s",
                "Avg cash share by account",
                "Index return by account span",
            ],
            rows,
        )

    # ------------------------------------------------------------- data checks
    def section_checks(self) -> str:
        r = self.r
        d = r.diagnostics
        audit = r.audit
        economic, quantity = audit["economic"], audit["quantity"]
        stamps = audit["stamps"]
        manual = stamps[stamps["source"] == "manual_import"]
        other = stamps[
            (stamps["source"] != "manual_import") & (stamps["market"] != "crypto")
        ]
        rows = [
            [
                "Rows equal on every economic field, flag already applied",
                f"Extra rows: {economic['extra_rows']}. Groups: {economic['groups']}. Orders: "
                f"{economic['orders']}",
                f"Orders summing to no more than their quantity: "
                f"{economic['orders_within_quantity']}. Over it: "
                f"{economic['orders_over_quantity']}. With no order row: "
                f"{economic['orders_without_row']}",
                "Kept. They are separate trades of one order, with the time cut to the minute",
                "**Export gap**: no `fill_seq` column, so trades cannot be told from repeats",
            ],
            [
                "Websocket row repeating the next day's reconciler row",
                f"Rows: {d['cross_day_twins_dropped']}",
                "Same order, side, quantity and price, recorded exactly 24h after its `filled_at`",
                f"Removed. Seed rows that absorbed one, reduced by the same quantity: "
                f"{d['seeds_corrected_for_twins']}",
                "Ledger data. The export copies `filled_at` as stored. The flag misses them because "
                "the stored day differs. Cause of the stamp: unverified",
            ],
            [
                "Order whose fills exceed its quantity",
                f"Orders before cleaning: {quantity['over_quantity_before']}. After: "
                f"{quantity['over_quantity_after']}",
                "The repeated rows above, plus an order with a row equal to its whole quantity",
                f"Rows removed as partials inside a running total: "
                f"{d['running_total_rows_dropped']}. Unresolved orders: "
                f"{d['overfilled_orders_unresolved']}",
                "Ledger data",
            ],
            [
                "Partial fill of an order with no order row, proven by inventory",
                f"Rows: {d['inventory_proven_partials_dropped']}",
                "The symbol's sells exceed all its holdings by exactly this row's quantity, and a "
                "later row of the order is larger",
                "Removed",
                "Ledger data",
            ],
            [
                "Order row behind the ledger",
                f"Orders: {quantity['row_says_less_than_ledger']}",
                "The row's filled quantity is below the ledger's, mostly `open` rows with no ending",
                "The ledger's quantity counts, and the outcome is corrected",
                "Ledger data",
            ],
            [
                "Ledger behind the order row",
                f"Fills: {quantity['row_says_more_than_ledger']}",
                "The row says filled and the ledger has less",
                "The residual is added as a fill at the row's price, on the order's date",
                "Ledger data. The export's only row filters are the time window and quarantine, and "
                "neither can remove these",
            ],
            [
                "`manual_import` rows, stamped on a weekend",
                f"On a weekend: {int(manual['weekend'].sum())} of {int(manual['rows'].sum())} rows",
                "`opening_lots.py` stamps the cutover instant and sets quantity to holdings minus "
                "net fills, at the broker's average cost. They are opening lots, not transfers",
                "No markout or cash flow. Return runs from the last close before the stamp",
                "Ledger data, documented. **Export gap**: nothing separates a seed from another "
                "manual import",
            ],
            [
                "Other equity rows stamped on a weekend",
                f"On a weekend: {int(other['weekend'].sum())} of {int(other['rows'].sum())} rows, "
                f"before removals",
                "One broker's rows can be stamped a day away from the session",
                "Mapped to the next session. Twins removed as above",
                "Ledger data",
            ],
            [
                "Fill far from its session's close",
                f"Rows: {len(r.implausible)}",
                f"More than {IMPLAUSIBLE_DEVIATION:.0%} from the close, at prices like an old "
                f"average cost",
                "Quantity kept, treated as opening lots, with no markout or cash flow",
                "Ledger data",
            ],
        ]
        out = [
            "Each check below comes from a question raised by the independent dots analysis of "
            "the same export. No check found a wrong value written by the export. Two columns it "
            "does not have are listed as gaps for the desk.\n\n",
            table(["Check", "Count", "Evidence", "Treatment", "Source"], rows),
            "\n**Join between the ledger and the order tables.** The two tables hold different "
            "populations, so neither is a denominator for the other. The hashed key itself works: "
            f"executions with no order row whose reference is on any order row of that account: "
            f"{audit['refs_elsewhere']}.\n\n",
        ]
        join_rows = []
        join = audit["join"]
        join = join.assign(label=[self.acct(a) for a in join["alias"]]).sort_values(
            ["market", "label"]
        )
        for row in join.to_dict("records"):
            join_rows.append(
                [
                    row["market"], row["label"], row["executed_orders"], row["with_order_row"],
                    share(row["with_order_row_share"]), share(row["notional_share"]),
                    row["before_first_order_row"], row["micro"], row["other_after_first_order_row"],
                    f"{row['filled_rows_with_ledger']} of {row['filled_order_rows']}",
                ]
            )  # fmt: skip
        out.append(
            table(
                [
                    "Market",
                    "Account",
                    "Executed orders",
                    "With order row",
                    "Share of orders",
                    "Share of notional",
                    "Before the first order row",
                    "Recurring micro-buys",
                    "Other, after the first order row",
                    "Filled order rows that have ledger fills",
                ],
                join_rows,
            )  # fmt: skip
        )
        return "".join(out)

    def section_denominators(self) -> str:
        r = self.r
        stages = r.diagnostics["stages"]
        rows = []
        for market, name, _currency in MARKETS:
            final = r.fills[(r.fills["market"] == market)]
            trades = int((~final["seed"]).sum())
            seeds = int(stages["seed_rows"].get(market, 0))
            moved = sum(1 for i in r.implausible if i["market"] == market)
            exported = stages["exported_rows"].get(market, 0)
            unflagged = stages["unflagged_rows"].get(market, 0)
            twins = unflagged - stages["after_twins"].get(market, 0)
            totals = stages["after_twins"].get(market, 0) - stages["after_totals"].get(
                market, 0
            )
            partials = stages["after_totals"].get(market, 0) - stages[
                "after_partials"
            ].get(market, 0)
            added = stages["after_order_only"].get(market, 0) - stages[
                "after_partials"
            ].get(market, 0)
            rows.append(
                [
                    name, exported, exported - unflagged, seeds, twins, totals, partials, moved, added,
                    trades,
                ]
            )  # fmt: skip
        fills_table = table(
            [
                "Market", "Ledger rows", "Flagged superseded", "Opening seeds", "Repeats",
                "Inside a running total", "Proven by inventory", "Cannot be trades", "Added from order rows", "Fills used",
            ],
            rows,
        )  # fmt: skip
        order_rows = [
            [
                {m: n for m, n, _c in MARKETS}[row["market"]],
                row["rows"],
                row["cancel_rows"],
                row["rejected"],
                row["never_accepted"],
                row["parking"],
                row["accepted"],
            ]  # fmt: skip
            for row in r.audit["orders"].to_dict("records")
            if row["market"] in {m for m, _n, _c in MARKETS}
        ]
        orders_table = table(
            [
                "Market", "Order rows", "Cancel rows", "Rejected", "Never accepted",
                "Parking instruments", "Accepted orders used",
            ],
            order_rows,
        )  # fmt: skip
        return (
            "Fills, from every live ledger row to the fills every section uses. Each row leaves "
            "at one step, and the steps add up to the last column:\n\n"
            f"{fills_table}\n"
            "Orders, from every live order row to the fill-rate denominator:\n\n"
            f"{orders_table}"
        )

    def section_sensitivity(self) -> str:
        rows = []
        for scenario in self.r.scenarios:
            cells: list[str] = [scenario.name]
            for market, _name, _currency in MARKETS:
                lots = scenario.lots[scenario.lots["market"] == market]
                cost = lots["base_cost"].sum() if len(lots) else 0.0
                if cost:
                    ret = lots["pnl"].sum() / cost
                    index_ret = lots["index_pnl"].sum() / cost
                    cells.append(
                        f"{pct(ret)} ({pct(ret - index_ret)} vs index, {lots['lot'].nunique()} lots)"
                    )
                else:
                    cells.append("n/a")
            if scenario.cash_shares is None:
                cells.append("as base")
            else:
                ordered = sorted(
                    scenario.cash_shares.items(),
                    key=lambda kv: (
                        [m for m, _n, _c in MARKETS].index(kv[0][1]),
                        self.acct(kv[0][0]),
                    ),
                )
                cells.append(" / ".join(share(value) for _key, value in ordered))
            rows.append(cells)
        return (
            "Return on lots bought in the window, under each treatment that could be argued. Only "
            "the named treatment changes on each line. Cash shares run KR, US, then crypto, in "
            "account-label order.\n\n"
            + table(
                ["Treatment", "KR", "US", "Crypto", "Avg cash share by account"], rows
            )
        )

    def render(self) -> str:
        r = self.r
        window = r.manifest["window"]
        tables = r.manifest["tables"]
        scope = (
            "This committed version holds ratios and counts only. Symbols, amounts and account "
            "names are masked because the repository is public and the export is private trading "
            "data. The full version is outside the repository."
            if self.public
            else "**Private: do not commit.** Full version with symbols, amounts and account names."
        )
        parts = [
            "# ROB-1439: retrospective of real buys and sells\n\n",
            f"{scope}\n\n",
            "This report states measured facts for the operator's later policy decision. It "
            "recommends nothing.\n\n",
            "## Reproduce\n\n",
            f"```bash\n{RUN_COMMAND}\n```\n\n",
            "Add `--check` to compare against the files on disk without writing. Every number "
            "comes from `research/trade_retro/`: section 2 from `positions.py`, section 3 from "
            "`entry.py` and `markouts.py`, section 4 from `idle.py`, section 5 from `rules.py`, "
            "the checks from `audit.py` and the sensitivity table from `sensitivity.py`, all rendered "
            "by `report.py`.\n\n",
            f"- Export: `dots-history-export/v1`, window {window['since_utc'][:10]} to "
            f"{window['until_utc'][:10]}, `manifest.json` sha256 `{r.manifest_sha256}`, "
            f"{sum(t['row_count'] for t in tables.values())} rows, not committed.\n",
            f"- Price snapshot: `prices_manifest.json` sha256 `{r.book.snapshot_sha256}`, fetched "
            f"{r.book.meta['fetched_at_utc'][:10]}, not committed. Re-fetching can pick up revised "
            f"prices, so it goes to a new directory and produces a new report.\n",
            "- Prices: Yahoo Finance daily bars for KR (KRX regular session) and US, Upbit public "
            "daily candles for crypto. Benchmarks: "
            + ", ".join(BENCHMARK_NAMES[m] for m, _, _ in MARKETS)
            + ". The crypto benchmark is one coin, not a market index, because no public crypto "
            "index with free daily history was used.\n\n",
            "## Headline\n\n",
            self.section_headline(),
            "\nReturns are price return before fees. Each line's definitions are in the sections "
            "below. An idle share is a lower-bound estimate, so read the data limits first.\n\n",
            "## Method\n\n",
            *_method_text(),
            "## Data checks\n\n",
            self.section_checks(),
            "\n## Denominators and exclusions\n\n",
            self.section_denominators(),
            "\n## Sensitivity\n\n",
            self.section_sensitivity(),
            "\n",
        ]
        for market, name, currency in MARKETS:
            parts += [
                f"## {name}\n\nAmounts are in {currency}. Index: {BENCHMARK_NAMES[market]}.\n\n",
                f"### {name}: 2. Positions, against cash and the index\n\n",
                self.section_positions(market, currency),
                f"\n### {name}: 3. Entry types, fill rates and markouts\n\n",
                self.section_entry(market),
                f"\n### {name}: 4. Idle cash\n\n",
                self.section_idle(market, currency),
                f"\n### {name}: 5. Largest winners and losers, and candidate rules\n\n",
                self.section_winners(market, currency),
                "**Candidate rules.** In-sample: each number was computed on the same six months "
                "it would have changed, so none of them predicts anything.\n\n",
                self.section_rules(market, currency),
                "\n",
            ]
        parts += ["## Data limits\n\n", self.section_limits()]
        return "".join(parts)


def _short_tag(tag: str) -> str:
    return tag[:12] + "…" if tag.startswith("oth_") else tag


def _method_text() -> list[str]:
    return [
        "- **Scope.** Real-money order tables and `execution_ledger` only, with "
        "`superseded_duplicate = false`. A fill is the union of the ledger and the order row, "
        "taking the larger quantity for each order.\n",
        "- **Position.** One account's holding in one symbol from flat to flat, matched first in, "
        "first out. An open quantity is marked at the last daily close. Returns are weighted by "
        "cost. When a sell proves older holdings existed, those go first in the queue, so selling "
        "old shares does not use up a lot bought in the window.\n",
        "- **Lots bought in the window** return from the fill price. **Opening lots** (seed "
        "rows) return from the close on the seed date, because their broker cost predates the "
        "window. Each is reported separately, and only the first is a judgement of this window's "
        "buying.\n",
        "- **Against cash.** Cash is assumed to earn 0%, so the return is also the excess over "
        "cash. **Against the index.** The same money in the benchmark from each lot's entry date "
        "to its exit or mark date.\n",
        f"- **Entry type.** `deep_limit` is a limit at least {DEEP_PCT:.0f}% from the last "
        "complete daily close, below it for a buy and above it for a sell, matching the near "
        "edge of `buy.deep_limit_pct_range`. `near_market` is anything closer, including market "
        "orders. The suffix `/proposal` means a proposal rung links to the order, and `/session` "
        "means an order row with no rung. `external` means a fill with no order row at all: an "
        "app trade or one from before the order ledger began. `external_micro` is the crypto "
        "subset at or below the small fixed size of a recurring buy programme.\n",
        "- **Fill rate.** Accepted orders are rows that reached the broker, less rejections and "
        "cancel-operation rows. An order is filled if any quantity filled. `Open` counts as "
        "unfilled in one column and is removed in the other. An **intent** is one account, symbol, "
        "side and local ISO week, so a resubmitted order counts once.\n",
        "- **Markout.** One row per executed order at its volume-weighted price, to the close 1, "
        "5 and 20 sessions after the fill's session, signed so a positive value favours the trade.\n",
        "- **Idle cash.** Opening cash is the smallest amount keeping cash at or above zero, "
        "assuming no external flows. Opening lots count as held from the account's first day, "
        "because they have no purchase date and no cash outflow. Cash share is cash over cash "
        "plus holdings at market, "
        "averaged by day from the account's first fill. Forgone is each day's cash times the "
        "next day's index return, not compounded, so a negative value is a loss that cash "
        "avoided. Parking instruments are SGOV, BIL, 459580 and 357870.\n\n",
    ]
