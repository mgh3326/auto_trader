"""Commands: ``collect`` (public network fetch) and ``build`` (offline).

    uv run python -m research.kr_delist_repair.cli collect --out DIR [--asof YYYY-MM-DD]
    uv run python -m research.kr_delist_repair.cli build --out DIR --repo-data DIR

``collect`` fetches only public KIND pages and the public KIS master files and
records each body with its SHA-256.  ``build`` makes no network call: it reads
those raw files and the kr-corpus-v1 main scope, then writes the resolution
table, join audit, coverage report, the causal export and its manifest.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from research.kr_delist_repair import kind_parse
from research.kr_delist_repair.corpus_read import CorpusPaths
from research.kr_delist_repair.export import (
    build_panel,
    sha256_file,
    write_manifest,
    write_panel,
)
from research.kr_delist_repair.kind_fetch import (
    KIND_MARKET_CODES,
    KIS_MASTER_FILES,
    MARKET_DIVISIONS,
    KindFetcher,
    RawStore,
    decode_kind,
)
from research.kr_delist_repair.model import KindDelistRow, KindDisclosure
from research.kr_delist_repair.resolve import (
    Activity,
    Presence,
    Resolution,
    payout_counts,
    resolve_all,
)

KIND_LIST_FROM = date(2014, 1, 1)
NOTICE_TITLE = re.compile(r"^(\[정정\])?상장폐지\s*(\(|$)")
DESK_MEASUREMENT = {
    "source": "handoffkeep doc report/2026-09-15/backtest-data-plan (id 1925), operator-desk",
    "measured_at": "2026-09-15",
    "kr_symbol_universe_rows": 4011,
    "kr_symbol_universe_active": 3940,
    "kr_symbol_universe_delist_date_filled": 12,
    "kr_symbol_universe_krx_trading_suspended": 129,
    "corpus_symbols": 3143,
    "corpus_not_trading_today": 540,
    "corpus_absent_from_universe": 491,
}


@dataclass(frozen=True)
class Sources:
    master: dict[str, dict[str, str]]
    kind_rows: list[KindDelistRow]
    disclosures: list[KindDisclosure]
    asof: date


# --------------------------------------------------------------------------- corpus


def presences(
    membership: pa.Table, bar_symbols: set[str]
) -> tuple[dict[str, Presence], list[date]]:
    """Split each code's membership into contiguous segments, keyed by listing id.

    A segment ends where the code misses a calendar session and later returns
    (code reuse after a delisting); each segment is a separate listing.
    """
    calendar = sorted(
        date.fromisoformat(s)
        for s in pc.unique(membership.column("session")).to_pylist()
    )
    index = {d.isoformat(): i for i, d in enumerate(calendar)}
    sessions: dict[str, list[int]] = defaultdict(list)
    markets: dict[tuple[str, int], set[str]] = defaultdict(set)
    rows = zip(
        membership.column("ticker").to_pylist(),
        membership.column("session").to_pylist(),
        membership.column("market").to_pylist(),
        strict=True,
    )
    for sym, ses, mkt in rows:
        sessions[sym].append(index[ses])
        markets[(sym, index[ses])].add(mkt)
    out: dict[str, Presence] = {}
    for sym, idx in sessions.items():
        idx.sort()
        start = prev = idx[0]
        segment = 0
        for i in [*idx[1:], None]:
            if i is not None and i == prev + 1:
                prev = i
                continue
            seg_markets = sorted(
                {m for k in range(start, prev + 1) for m in markets.get((sym, k), ())}
            )
            p = Presence(
                symbol=sym,
                first_session=calendar[start],
                last_session=calendar[prev],
                markets=tuple(seg_markets),
                has_bars=sym in bar_symbols,
                segment=segment,
            )
            out[p.listing_id] = p
            if i is not None:
                start = prev = i
                segment += 1
    return out, calendar


def activities(
    targets: list[Presence], membership: pa.Table, bars: pa.Table, no_trade: pa.Table
) -> dict[str, Activity]:
    symbols = sorted({p.symbol for p in targets})

    def filt(t: pa.Table) -> pa.Table:
        return t.filter(pc.is_in(t.column("ticker"), pa.array(symbols)))

    fm, fb, fn = filt(membership), filt(bars), filt(no_trade)
    mem: dict[str, list[str]] = defaultdict(list)
    for s, t in zip(
        fm.column("session").to_pylist(), fm.column("ticker").to_pylist(), strict=True
    ):
        mem[t].append(s)
    traded: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for s, t, c in zip(
        fb.column("session").to_pylist(),
        fb.column("ticker").to_pylist(),
        fb.column("close").to_pylist(),
        strict=True,
    ):
        traded[t].append((s, c))
    nts: dict[str, set[str]] = defaultdict(set)
    for s, t in zip(
        fn.column("session").to_pylist(), fn.column("ticker").to_pylist(), strict=True
    ):
        nts[t].add(s)
    out: dict[str, Activity] = {}
    for p in targets:
        lo, hi = p.first_session.isoformat(), p.last_session.isoformat()
        sessions = sorted(s for s in mem.get(p.symbol, []) if lo <= s <= hi)
        nt = {s for s in nts.get(p.symbol, set()) if lo <= s <= hi}
        run = best = 0
        for s in sessions:
            run = run + 1 if s in nt else 0
            best = max(best, run)
        tr = sorted(x for x in traded.get(p.symbol, []) if lo <= x[0] <= hi)
        out[p.listing_id] = Activity(
            last_traded_session=date.fromisoformat(tr[-1][0]) if tr else None,
            no_trade_sessions=len(nt),
            no_trade_in_final_20=sum(1 for s in sessions[-20:] if s in nt),
            longest_no_trade_run=best,
            last_close=tr[-1][1] if tr else None,
        )
    return out


# --------------------------------------------------------------------------- collect


def _kind_rows_from_store(store: RawStore, asof: date) -> list[KindDelistRow]:
    rows: list[KindDelistRow] = []
    for market in KIND_MARKET_CODES:
        name = f"kind_delist/{market}_{KIND_LIST_FROM:%Y%m%d}_{asof:%Y%m%d}.xls"
        rows.extend(
            kind_parse.parse_delist_excel(decode_kind(store.read(name)), market)
        )
    return rows


def _master_from_store(store: RawStore) -> dict[str, dict[str, str]]:
    return {
        market: kind_parse.parse_kis_master(
            store.read(f"kis_master/{zip_name}"), suffix
        )
        for market, (zip_name, suffix) in KIS_MASTER_FILES.items()
    }


def _search_window(p: Presence, calendar: list[date], asof: date) -> tuple[date, date]:
    if p.last_session == calendar[-1]:
        return date(2025, 1, 1), asof
    end = min(p.last_session + timedelta(days=60), asof)
    return end - timedelta(days=365 * 2), end


def collect(out: Path, asof: date) -> None:
    store = RawStore(out / "raw")
    fetcher = KindFetcher(store)
    try:
        for market in KIS_MASTER_FILES:
            fetcher.kis_master(market)
        for market in KIND_MARKET_CODES:
            fetcher.delist_list(market, KIND_LIST_FROM, asof)
        master = _master_from_store(store)
        listed = set(master["KOSPI"]) | set(master["KOSDAQ"])
        kind_symbols = {r.symbol for r in _kind_rows_from_store(store, asof)}

        corpus = CorpusPaths()
        membership = corpus.membership()
        bar_symbols = set(pc.unique(corpus.bars().column("ticker")).to_pylist())
        pres, calendar = presences(membership, bar_symbols)
        need = [
            p
            for p in pres.values()
            if (p.symbol not in listed or p.last_session < calendar[-1])
            and p.symbol not in kind_symbols
        ]
        notices: list[dict[str, str]] = []
        for p in sorted(need, key=lambda x: x.symbol):
            parent = p.symbol[:5] + "0"
            lo, hi = _search_window(p, calendar, asof)
            page = decode_kind(fetcher.disclosure_search(parent, lo, hi))
            for (
                acptno,
                filed_at,
                company,
                title,
                filer,
            ) in kind_parse.parse_disclosure_search(page):
                if filer not in MARKET_DIVISIONS or not NOTICE_TITLE.match(title):
                    continue
                viewer = decode_kind(fetcher.disclosure_viewer(acptno))
                for docno in kind_parse.parse_viewer_docnos(viewer):
                    contents = decode_kind(fetcher.disclosure_contents(docno))
                    url = kind_parse.parse_content_url(contents)
                    fetcher.disclosure_body(docno, url)
                    notices.append(
                        {
                            "target": p.symbol,
                            "acptno": acptno,
                            "filed_at": filed_at,
                            "company": company,
                            "title": title,
                            "docno": docno,
                            "body": f"kind_body/{docno}.htm",
                        }
                    )
        payload = json.dumps(
            notices, ensure_ascii=False, indent=1, sort_keys=True
        ).encode()
        store.put(
            "kind_notices.json", payload, "derived:collect", {"asof": asof.isoformat()}
        )
        (out / "collect_meta.json").write_text(
            json.dumps(
                {
                    "asof": asof.isoformat(),
                    "requests": fetcher.requests,
                    "targets_searched": len(need),
                },
                indent=2,
            )
            + "\n"
        )
        print(
            f"collect: requests={fetcher.requests} notice_bodies={len(notices)} searched={len(need)}"
        )
    finally:
        fetcher.close()


# --------------------------------------------------------------------------- build


def load_sources(out: Path) -> Sources:
    store = RawStore(out / "raw")
    asof = date.fromisoformat(
        json.loads((out / "collect_meta.json").read_text())["asof"]
    )
    disclosures: list[KindDisclosure] = []
    seen: set[str] = set()
    for n in json.loads(store.read("kind_notices.json")):
        if n["docno"] in seen:
            continue
        seen.add(n["docno"])
        for rec in kind_parse.parse_disclosure_body(
            decode_kind(store.read(n["body"])), n["acptno"], n["filed_at"]
        ):
            if n["company"] and not rec.issue_name.startswith(n["company"]):
                rec = replace(rec, issue_name=f"{n['company']} {rec.issue_name}")
            disclosures.append(rec)
    return Sources(
        _master_from_store(store), _kind_rows_from_store(store, asof), disclosures, asof
    )


def _write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _code_tree_clean() -> bool:
    status = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "research/kr_delist_repair",
            ":!research/kr_delist_repair/data",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return status.strip() == ""


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def build(out: Path, repo_data: Path) -> dict[str, object]:
    src = load_sources(out)
    listed = set(src.master["KOSPI"]) | set(src.master["KOSDAQ"])
    names = {**src.master["KOSPI"], **src.master["KOSDAQ"]}

    corpus = CorpusPaths()
    membership = corpus.membership()
    bars = corpus.bars()
    no_trade = corpus.no_trade()
    bar_symbols = set(pc.unique(bars.column("ticker")).to_pylist())
    pres, calendar = presences(membership, bar_symbols)
    main_end = calendar[-1]

    latest: dict[str, Presence] = {}
    for p in pres.values():
        if p.symbol not in latest or p.segment > latest[p.symbol].segment:
            latest[p.symbol] = p
    # Listing ids. "Not listed" = the latest listing of a code absent from today's master.
    not_listed = {p.listing_id for s, p in latest.items() if s not in listed}
    ended = {lid for lid, p in pres.items() if p.last_session < main_end}
    targets = [pres[lid] for lid in sorted(not_listed | ended)]
    act = activities(targets, membership, bars, no_trade)
    resolutions = resolve_all(
        targets=targets,
        calendar=calendar,
        activity=act,
        kind_rows=src.kind_rows,
        disclosures=src.disclosures,
        master_names=names,
    )
    for r in resolutions:
        notes = [r.identity_note]
        if r.segment > 0 or latest[r.symbol].segment > 0:
            notes.append(
                f"code_reused:segment_{r.segment}_of_{latest[r.symbol].segment}"
            )
        if r.symbol in listed:
            notes.append(f"code_listed_today_as:{names[r.symbol]}")
        r.identity_note = "|".join(x for x in notes if x)

    panel = build_panel(membership, bars, no_trade, resolutions, pres)
    export_path = out / "export" / "kr_daily_causal.v1.parquet"
    write_panel(panel, export_path)

    res_rows = [r.row() for r in resolutions]
    res_path = repo_data / "delist_resolution.v1.csv"
    _write_csv(res_path, res_rows, list(res_rows[0]))
    unresolved = [
        {
            "listing_id": r.listing_id,
            "symbol": r.symbol,
            "name": r.name,
            "in_denominator": r.listing_id in not_listed and r.in_bar_frame,
            "corpus_last_session": r.corpus_last_session,
            "unresolved_reason": r.unresolved_reason,
        }
        for r in resolutions
        if not r.resolved
    ]
    unresolved_path = repo_data / "unresolved.v1.csv"
    _write_csv(
        unresolved_path,
        unresolved,
        [
            "listing_id",
            "symbol",
            "name",
            "in_denominator",
            "corpus_last_session",
            "unresolved_reason",
        ],
    )

    coverage = coverage_summary(resolutions, pres, not_listed, ended)
    audit = join_audit(latest, pres, listed, src, calendar, resolutions)
    (repo_data / "join_audit.md").write_text(render_audit(audit))
    (repo_data / "coverage_report.md").write_text(
        render_coverage(coverage, resolutions, unresolved)
    )

    raw_index = out / "raw" / "index.json"
    terminal_counts = Counter(
        v for v in panel.column("terminal_outcome").to_pylist() if v is not None
    )
    manifest = write_manifest(
        out / "export" / "manifest.json",
        {
            "kr_daily_causal": export_path,
            "delist_resolution": res_path,
            "unresolved": unresolved_path,
            "raw_index": raw_index,
        },
        {
            "artifact": "kr-delist-repair-v1",
            "issue": "ROB-1430",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "code_commit": _git_head(),
            "code_tree_clean": _code_tree_clean(),
            "kind_asof": src.asof.isoformat(),
            "inputs": {
                "kr_corpus_v1_manifest_sha256": corpus.manifest_sha256(),
                "clamp_admit_v1_manifest_sha256": corpus.clamp_manifest_sha256(),
                "raw_index_sha256": sha256_file(raw_index),
            },
            "scope": {
                "first_session": calendar[0].isoformat(),
                "last_session": main_end.isoformat(),
                "holdout_read": False,
            },
            "conventions": {
                "bar_available_at": "session 16:00 Asia/Seoul (stored UTC)",
                "terminal_available_at": "KRX notice filing time if a notice body was used, else delist_date 00:00 Asia/Seoul",
                "listing_id": "symbol#segment; a code whose membership stops and later restarts is a new listing",
                "terminal_row": "last membership session of a listing whose membership ended in main scope; never forward-filled",
                "unresolved_terminal": "terminal_outcome=unresolved, no delist_date",
            },
            "rows": panel.num_rows,
            "symbols": len(latest),
            "listings": len(pres),
            "terminal_outcome_counts": dict(sorted(terminal_counts.items())),
            "coverage": coverage,
        },
    )
    # Committed copy without local absolute paths.
    portable = json.loads(json.dumps(manifest))
    for entry in portable["files"].values():
        entry["path"] = Path(entry["path"]).name
    (repo_data / "export_manifest.json").write_text(
        json.dumps(portable, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return coverage


# --------------------------------------------------------------------------- reports


def coverage_summary(
    rows: list[Resolution],
    pres: dict[str, Presence],
    not_listed: set[str],
    ended: set[str],
) -> dict[str, object]:
    by = {r.listing_id: r for r in rows}

    def frame(symbols: set[str]) -> dict[str, object]:
        resolved = sum(1 for s in symbols if by[s].resolved)
        return {
            "denominator": len(symbols),
            "resolved": resolved,
            "unresolved": len(symbols) - resolved,
            "coverage": round(resolved / len(symbols), 6) if symbols else None,
        }

    bar_frame = {s for s in not_listed if pres[s].has_bars}
    return {
        "primary_delisted_frame": "corpus symbols with bars absent from today's KIS master (desk's 540 definition)",
        "primary": frame(bar_frame),
        "membership_frame_not_listed": frame(not_listed),
        "ended_in_main_scope": frame(ended),
        "date_basis": dict(
            Counter(by[s].date_basis for s in bar_frame if by[s].resolved)
        ),
        "terminal_outcome": dict(
            sorted(
                Counter(
                    by[s].terminal_outcome for s in bar_frame if by[s].resolved
                ).items()
            )
        ),
        "payout_semantics": payout_counts([by[s] for s in bar_frame]),
        "corpus_date_check": dict(
            Counter(by[s].corpus_date_check for s in bar_frame if by[s].resolved)
        ),
    }


def join_audit(
    pres: dict[str, Presence],
    listings: dict[str, Presence],
    listed: set[str],
    src: Sources,
    calendar: list[date],
    rows: list[Resolution],
) -> dict[str, object]:
    bar = {s for s, p in pres.items() if p.has_bars}
    main_end = calendar[-1]
    not_listed = {s for s in bar if s not in listed}
    alnum = sorted(s for s in pres if not s.isdigit())
    reused = sorted(r.symbol for r in rows if "code_listed_today_as" in r.identity_note)
    segmented = sorted({p.symbol for p in listings.values() if p.segment > 0})
    multi = sorted(s for s, p in pres.items() if len(p.markets) > 1)
    kind_codes = {r.symbol for r in src.kind_rows}
    return {
        "desk_measurement": DESK_MEASUREMENT,
        "kind_asof": src.asof.isoformat(),
        "master_issues": {
            "KOSPI": len(src.master["KOSPI"]),
            "KOSDAQ": len(src.master["KOSDAQ"]),
            "total": len(listed),
        },
        "corpus_membership_symbols": len(pres),
        "corpus_bar_symbols": len(bar),
        "membership_only_symbols": sorted(set(pres) - bar),
        "bar_symbols_in_master": len(bar & listed),
        "bar_symbols_not_in_master": len(not_listed),
        "not_in_master_ended_in_main": sum(
            1 for s in not_listed if pres[s].last_session < main_end
        ),
        "not_in_master_alive_at_main_end": sum(
            1 for s in not_listed if pres[s].last_session == main_end
        ),
        "not_in_master_preferred": sum(1 for s in not_listed if not s.endswith("0")),
        "not_in_master_in_kind_list": len(not_listed & kind_codes),
        "ended_in_main_but_listed_today": reused,
        "codes_with_more_than_one_listing": segmented,
        "multi_market_symbols": multi,
        "alphanumeric_codes": alnum,
        "kind_rows": len(src.kind_rows),
        "kind_notice_bodies": len(src.disclosures),
        "date_basis": dict(
            Counter(r.date_basis for r in rows if r.symbol in not_listed and r.resolved)
        ),
    }


def render_audit(a: dict[str, object]) -> str:
    d = a["desk_measurement"]
    assert isinstance(d, dict)
    lines = [
        "# Join audit — kr-corpus-v1 ⋈ kr_symbol_universe (ROB-1430)",
        "",
        "Generated by `research/kr_delist_repair/cli.py build`. No database was read: the",
        "production DB is out of bounds for this unit, so the table's own counts are quoted",
        "from the operator-desk measurement and the table's *source* is re-measured instead.",
        "",
        "## 1. The two denominators",
        "",
        "| Side | Count | Where it comes from |",
        "|---|---:|---|",
        f"| `kr_symbol_universe` rows | {d['kr_symbol_universe_rows']:,} | desk measurement {d['measured_at']} ({d['source']}) |",
        f"| … active | {d['kr_symbol_universe_active']:,} | same |",
        f"| … `delist_date` filled | {d['kr_symbol_universe_delist_date_filled']} | same |",
        f"| KIS master issues today (KOSPI {a['master_issues']['KOSPI']:,} + KOSDAQ {a['master_issues']['KOSDAQ']:,}) | {a['master_issues']['total']:,} | public `kospi_code.mst.zip` / `kosdaq_code.mst.zip`, fetched {a['kind_asof']} — the files `app/services/kr_symbol_universe_service.py` syncs the table from |",
        f"| corpus symbols with bars | {a['corpus_bar_symbols']:,} | kr-corpus-v1 main scope 2015-01-02..2024-12-30 |",
        f"| corpus symbols with membership | {a['corpus_membership_symbols']:,} | adds {len(a['membership_only_symbols'])} membership-only codes: {', '.join(a['membership_only_symbols'])} |",
        "",
        "## 2. Join result",
        "",
        "| Corpus (bar frame) | Count |",
        "|---|---:|",
        f"| present in today's master (join hits) | {a['bar_symbols_in_master']:,} |",
        f'| absent from today\'s master (the "delisted" frame) | {a["bar_symbols_not_in_master"]:,} |',
        f"| … membership ended inside the main scope | {a['not_in_master_ended_in_main']} |",
        f"| … alive on 2024-12-30, delisted later | {a['not_in_master_alive_at_main_end']} |",
        f"| … preferred / non-common codes | {a['not_in_master_preferred']} |",
        f"| … found by exact code in the KIND 상장폐지현황 list | {a['not_in_master_in_kind_list']} |",
        "",
        f"Desk count on {d['measured_at']}: {d['corpus_not_trading_today']} not trading, {d['corpus_absent_from_universe']} absent from the table.",
        f"Today's {a['bar_symbols_not_in_master']} is larger because KIND lists further 2026 delistings after that date.",
        "",
        "## 3. Findings",
        "",
        "1. **The join is structurally lossy.** The table is synced from the *current* master files, so a",
        "   symbol delisted before the sync began never had a row. Filling `delist_date` in the table cannot",
        f"   repair the ~{d['corpus_absent_from_universe']} corpus symbols that have no row at all; an inner join drops",
        "   them and reintroduces survivorship bias. Research must key lifecycle data by symbol from",
        "   `delist_resolution.v1.csv`, never through `kr_symbol_universe`.",
        f"2. **12/{d['kr_symbol_universe_rows']:,} is not a coverage number for the corpus.** The 12 dated rows are rows that",
        "   were once in the master; the corpus' delisted frame mostly lies outside the table.",
        "3. **KIND's company list omits preferred-share delistings.** Every corpus symbol ended inside the",
        "   main scope that is missing from the KIND list is a preferred / non-common code; they are resolved",
        "   from KRX `상장폐지(…)` notice bodies (exact short code). Date basis in the delisted frame:",
        f"   {', '.join(f'{k} {v}' for k, v in sorted(a['date_basis'].items()))}.",
        "4. **Key format is compatible.** Both sides use six-character codes and the table's sync keeps",
        "   alphanumeric codes (`_normalize_symbol_or_none`); the corpus has",
        f"   {len(a['alphanumeric_codes'])} of them: {', '.join(a['alphanumeric_codes'])}.",
        f"5. **Code reuse.** Codes whose membership stops and later restarts: {', '.join(a['codes_with_more_than_one_listing']) or 'none'};",
        "   each run is a separate `listing_id` (`symbol#segment`) with its own terminal row. Listings that",
        f"   ended in the corpus while the code is listed today: {', '.join(a['ended_in_main_but_listed_today']) or 'none'}.",
        "   A join on code alone would attach today's company to the old history.",
        f"6. **Market transfers.** {len(a['multi_market_symbols'])} symbols appear in both markets (KOSDAQ→KOSPI moves keep the code): {', '.join(a['multi_market_symbols'])}.",
        "",
        f"KIND rows parsed: {a['kind_rows']}; KRX notice issue records parsed: {a['kind_notice_bodies']}.",
        "",
    ]
    return "\n".join(lines)


def render_coverage(
    cov: dict[str, object], rows: list[Resolution], unresolved: list[dict[str, object]]
) -> str:
    p = cov["primary"]
    assert isinstance(p, dict)
    lines = [
        "# Coverage report — KR delisting / terminal outcome (ROB-1430)",
        "",
        f"Primary frame: {cov['primary_delisted_frame']}.",
        "",
        "| Frame | Denominator | Resolved | Unresolved | Coverage |",
        "|---|---:|---:|---:|---:|",
    ]
    for key in ("primary", "membership_frame_not_listed", "ended_in_main_scope"):
        f = cov[key]
        assert isinstance(f, dict)
        lines.append(
            f"| {key} | {f['denominator']} | {f['resolved']} | {f['unresolved']} | {f['coverage']:.2%} |"
        )
    lines += [
        "",
        "Resolved = delist date from a public KIND/KRX source, terminal outcome in the closed vocabulary,",
        "and (for symbols whose membership ended in 2015–2024) the date equals the next corpus session.",
        "",
        "## Date basis (primary frame)",
        "",
        *[f"- {k}: {v}" for k, v in sorted(cov["date_basis"].items())],  # type: ignore[union-attr]
        "",
        "## Corpus date check (primary frame)",
        "",
        *[f"- {k}: {v}" for k, v in sorted(cov["corpus_date_check"].items())],  # type: ignore[union-attr]
        "",
        "## Terminal outcome (primary frame)",
        "",
        *[f"- {k}: {v}" for k, v in cov["terminal_outcome"].items()],  # type: ignore[union-attr]
        "",
        "## Payout semantics (primary frame; a labelling hint, not an amount)",
        "",
        *[f"- {k}: {v}" for k, v in cov["payout_semantics"].items()],  # type: ignore[union-attr]
        "",
        "## Unresolved symbols",
        "",
    ]
    if unresolved:
        lines += [
            "| symbol | name | in primary frame | last corpus session | reason |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {u['symbol']} | {u['name']} | {u['in_denominator']} | {u['corpus_last_session']} | {u['unresolved_reason']} |"
            for u in unresolved
        ]
    else:
        lines.append("None.")
    lines += [
        "",
        "## Known limits",
        "",
        "- Listing dates before 2015-01-02 are censored (`listed_before_corpus_start`); later ones are the first",
        "  KRX membership session.",
        "- Suspensions are the corpus' no-trade sessions (open=high=low=0, close>0); KRX halt notices are not",
        "  collected.",
        "- Corporate actions are those implied by the delisting reason (merger, share exchange, conversion).",
        "  Splits and rights issues are already in the adjusted prices and are not re-derived.",
        "- `dissolution` on KOSPI covers both absorption mergers and fund liquidations; payout stays `ambiguous`.",
        "- Symbols delisted in 2025–2026 cannot be checked against the corpus: the holdout stays sealed.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kr_delist_repair")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--asof", type=date.fromisoformat, default=date.today())
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--repo-data", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "collect":
        collect(args.out, args.asof)
    else:
        coverage = build(args.out, args.repo_data)
        print(json.dumps(coverage, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
