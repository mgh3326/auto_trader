#!/usr/bin/env python3
"""ROB-353 (PR1) — ported ROB-349 PIT Binance USD-M universe index builder.

Read-only PUBLIC data only (data.binance.vision S3 listing + monthly 1d klines for
non-live boundary detection) + a local fapi exchangeInfo dump. Emits the metadata-only
manifest (symbol + listing window + coverage/confidence) and a snapshot-hash sidecar.
NO raw OHLCV persisted. No keys, no orders, no scheduler. The network RUN is operator-
gated; CI exercises only the pure helpers.

Usage (operator):
    # 1) save exchangeInfo once (public):
    #    curl -s https://fapi.binance.com/fapi/v1/exchangeInfo > /tmp/pit_audit_exchangeinfo.json
    # 2) build:
    uv run --no-project python build_pit_universe.py \\
        --exchange-info /tmp/pit_audit_exchangeinfo.json \\
        --out data_manifests/pit_universe.v1.json
"""

import argparse
import io
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BASE = "https://data.binance.vision/data/futures/um/monthly"

RENAME = {
    "MATICUSDT": "POLUSDT",
    "RNDRUSDT": "RENDERUSDT",
    "AGIXUSDT": "FETUSDT(merge)",
    "OCEANUSDT": "FETUSDT(merge)",
}

_ctx = ssl.create_default_context()


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pit-audit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return r.read()


def list_symbols(prefix: str) -> list[str]:
    out: list[str] = []
    marker = ""
    while True:
        q: dict[str, str] = {"delimiter": "/", "prefix": prefix}
        if marker:
            q["marker"] = marker
        xml = _get(S3 + "?" + urllib.parse.urlencode(q)).decode()
        cps = re.findall(r"<Prefix>" + re.escape(prefix) + r"([^/<]+)/</Prefix>", xml)
        out.extend(cps)
        if "<IsTruncated>true</IsTruncated>" in xml and cps:
            marker = prefix + cps[-1] + "/"
        else:
            break
    return out


def months_listed(prefix: str, pat: str) -> list[str]:
    try:
        xml = _get(S3 + "?" + urllib.parse.urlencode({"prefix": prefix})).decode()
    except Exception:
        return []
    return sorted(set(re.findall(pat, xml)))


def expected_months(fm: str | None, lm: str | None) -> int:
    if not fm or not lm:
        return 0
    (y0, m0), (y1, m1) = ((int(s[:4]), int(s[5:7])) for s in (fm, lm))
    return (y1 - y0) * 12 + (m1 - m0) + 1


def boundary_active(
    symbol: str, month: str, which: str
) -> tuple[str | None, bool | None]:
    """Return (day, frozen) from a monthly 1d file: first/last day with volume>0."""
    url = f"{BASE}/klines/{symbol}/1d/{symbol}-1d-{month}.zip"
    try:
        raw = _get(url, timeout=60)
    except Exception:
        return (None, None)
    z = zipfile.ZipFile(io.BytesIO(raw))
    data = z.read(z.namelist()[0]).decode()
    rows = [r.split(",") for r in data.splitlines() if r and not r.lower().startswith("open_time")]
    days: list[tuple[str, float]] = []
    for r in rows:
        try:
            ms = int(r[0])
            d = datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()
            v = float(r[5])
            days.append((d, v))
        except Exception:
            pass
    if not days:
        return (None, None)
    traded = [d for d, v in days if v > 0]
    frozen = which == "last" and days[-1][1] == 0  # delisting-freeze tail
    if which == "first":
        return (traded[0] if traded else days[0][0], None)
    return (traded[-1] if traded else days[-1][0], frozen)


def classify(sym: str, live_perp: set[str], live_all: set[str]) -> str:
    if sym in live_perp:
        return "live"
    if sym in live_all:
        return "settling"  # in exchangeInfo but not trading-perp (delivery/close-only/pending)
    return "dead"


def build_row(sym: str, live_perp: set[str], live_all: set[str]) -> dict:
    km = months_listed(
        f"data/futures/um/monthly/klines/{sym}/1d/",
        re.escape(sym) + r"-1d-(\d{4}-\d{2})\.zip",
    )
    fm = months_listed(
        f"data/futures/um/monthly/fundingRate/{sym}/",
        re.escape(sym) + r"-fundingRate-(\d{4}-\d{2})\.zip",
    )
    status = classify(sym, live_perp, live_all)
    first_seen = km[0] if km else None
    last_seen = km[-1] if km else None
    exp = expected_months(first_seen, last_seen)
    kcov = round(len(km) / exp, 3) if exp else 0.0
    fexp = expected_months(fm[0], fm[-1]) if fm else 0
    fcov = round(len(fm) / fexp, 3) if fexp else 0.0
    # day-precise active interval + freeze detection: only for non-live (where it differs/matters)
    active_from = active_to = None
    frozen: bool | None = False
    if status != "live" and km:
        active_from, _ = boundary_active(sym, km[0], "first")
        active_to, frozen = boundary_active(sym, km[-1], "last")
    elif status == "live" and km:
        active_from, _ = boundary_active(sym, km[0], "first")
        active_to = "ongoing"
    # confidence + reason
    reasons: list[str] = []
    if status == "dead":
        reasons.append("delisted")
    if status == "settling":
        reasons.append("settling_or_close_only")
    if frozen:
        reasons.append("delisting_freeze_tail")
    if len(fm) == 0:
        reasons.append("no_funding_data")
    if exp and kcov < 0.95:
        reasons.append("kline_month_gaps")
    if sym in RENAME:
        reasons.append(f"rebranded->{RENAME[sym]}")
    if status == "live":
        conf = "high"
    elif kcov >= 0.95 and len(fm) > 0:
        conf = "high"
    elif kcov >= 0.8:
        conf = "medium"
    else:
        conf = "low"
    return {
        "symbol": sym,
        "status": status,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "active_from": active_from,
        "active_to": active_to,
        "kline_months": len(km),
        "kline_coverage": kcov,
        "funding_first": fm[0] if fm else None,
        "funding_last": fm[-1] if fm else None,
        "funding_months": len(fm),
        "funding_coverage": fcov,
        "source": "data.binance.vision/futures/um",
        "confidence": conf,
        "missing_data_reason": ";".join(reasons),
    }


def write_outputs(rows: list[dict], out_json: str) -> str:
    import pit_universe as pu

    m = pu.PITManifest.from_pit_index_records(rows)
    m.save(out_json)
    meta = {
        "schema_version": "pit_universe.v1",
        "snapshot_hash": m.snapshot_hash(),
        "symbol_count": len(m.listings),
        "source": "data.binance.vision/futures/um",
        "source_records": len(rows),
    }
    meta_path = out_json.replace(".json", ".meta.json")
    json.dump(meta, open(meta_path, "w"), indent=2)  # noqa: SIM115
    return m.snapshot_hash()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ROB-353 PIT Binance USD-M universe index.")
    parser.add_argument(
        "--exchange-info",
        default="/tmp/pit_audit_exchangeinfo.json",
        help="Path to local fapi exchangeInfo JSON dump.",
    )
    parser.add_argument(
        "--out",
        default="data_manifests/pit_universe.v1.json",
        help="Output manifest path.",
    )
    args = parser.parse_args(argv)

    ei = json.load(open(args.exchange_info))  # noqa: SIM115
    live_all = {s["symbol"] for s in ei["symbols"]}
    live_perp = {
        s["symbol"]
        for s in ei["symbols"]
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
    }

    arch = set(list_symbols("data/futures/um/monthly/klines/"))
    syms = sorted(arch)
    print(f"building PIT index for {len(syms)} symbols...")

    with ThreadPoolExecutor(max_workers=12) as ex:
        rows = list(ex.map(lambda s: build_row(s, live_perp, live_all), syms))
    rows.sort(key=lambda r: (r["status"], r["symbol"]))

    snap = write_outputs(rows, args.out)

    c = Counter(r["status"] for r in rows)
    cf = Counter(r["confidence"] for r in rows)
    print("status :", dict(c))
    print("conf   :", dict(cf))
    print("with funding:", sum(1 for r in rows if r["funding_months"] > 0), "/", len(rows))
    print(
        "freeze-tail detected:",
        sum(1 for r in rows if "delisting_freeze_tail" in r["missing_data_reason"]),
    )
    print(f"snapshot_hash: {snap}")
    print(f"manifest: {args.out}")
    print("\n-- sample DEAD rows alive in 2023+ (survivorship-relevant) --")
    for r in rows:
        if (
            r["status"] == "dead"
            and (r["last_seen"] or "") >= "2023-01"
            and r["symbol"].endswith("USDT")
            and "_" not in r["symbol"]
            and "BUSD" not in r["symbol"]
        ):
            print(
                f'  {r["symbol"]:14} {r["first_seen"]}..{r["last_seen"]}'
                f'  active {r["active_from"]}..{r["active_to"]}'
                f'  kcov={r["kline_coverage"]} fcov={r["funding_coverage"]}'
                f'  conf={r["confidence"]} [{r["missing_data_reason"]}]'
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
