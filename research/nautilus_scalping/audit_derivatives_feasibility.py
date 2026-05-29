#!/usr/bin/env python3
"""ROB-355 — Binance USD-M family 4/5 derivatives data feasibility probe.

Read-only PUBLIC data only (``data.binance.vision`` S3 listing + a few sample files).
Measures *coverage* — path existence, first/last date, file counts, sampled column
headers/granularity — for the candidate derivatives-native data families:

    funding rate, open interest (metrics), liquidation, trade/depth evidence.

It NEVER persists raw market data: ``--out`` writes only a small coverage summary
(counts / date-ranges / column names) to the gitignored ``results/`` artifact root.
No keys, no orders, no Demo, no live/mainnet trading endpoint, no scheduler, no DB.
The network RUN is operator-gated; CI exercises only the pure helpers below.

This is data feasibility ONLY — not a strategy, not a backtest. See the durable report
at ``docs/runbooks/rob-355-derivatives-data-feasibility.md``.

Usage (operator):
    cd research/nautilus_scalping
    uv run --no-project python audit_derivatives_feasibility.py
    uv run --no-project python audit_derivatives_feasibility.py --out
"""

from __future__ import annotations

import argparse
import io
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BASE = "https://data.binance.vision"

# Curated PIT probe set: live majors + delisted/dead symbols (survivorship proof).
# Drawn from the committed ROB-349/353 manifest; ``active_to`` is the delisting month
# (``None`` = live). The point is to prove dead symbols still carry funding+OI archives.
PROBE_SYMBOLS: tuple[tuple[str, str | None], ...] = (
    ("BTCUSDT", None),
    ("XRPUSDT", None),
    ("MATICUSDT", "2024-09"),
    ("RNDRUSDT", "2024-07"),
    ("EOSUSDT", "2025-05"),
    ("GALUSDT", "2024-07"),
)

_ctx = ssl.create_default_context()


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no network)
# --------------------------------------------------------------------------- #
def parse_range(items: list[str]) -> dict | None:
    """``{first, last, count}`` for a list of ISO date/month strings, else ``None``.

    ISO ``YYYY-MM`` / ``YYYY-MM-DD`` sort lexicographically, so ``min``/``max`` are
    chronological without date parsing.
    """
    uniq = sorted({i for i in items if i})
    if not uniq:
        return None
    return {"first": uniq[0], "last": uniq[-1], "count": len(uniq)}


def survivorship_ok(data_last: str | None, active_to: str | None) -> bool:
    """True if a (possibly delisted) symbol's archive reaches its delisting.

    ``active_to`` ``None`` means still-live → trivially OK. For a dead symbol the
    archive must extend to at least its delisting month/day (``data_last >=
    active_to``), otherwise the dead symbol would be missing from its own active
    window and the panel would be survivorship-biased. Lexicographic ISO compare.
    """
    if active_to is None:
        return data_last is not None
    if data_last is None:
        return False
    return data_last[: len(active_to)] >= active_to


def panel_starts_late(data_first: str | None, listed_from: str | None) -> bool:
    """True if the archive starts *after* listing (informational panel-bound flag)."""
    if not data_first or not listed_from:
        return False
    n = min(len(data_first), len(listed_from))
    return data_first[:n] > listed_from[:n]


@dataclass(frozen=True)
class FamilySignals:
    """Booleans the network RUN feeds into the deterministic verdict."""

    archive_present: bool
    raw_evidence: bool  # raw exchange data (vs estimated/aggregated/banded proxy)
    delisted_covered: bool


def classify_verdict(s: FamilySignals) -> str:
    """Map coverage signals to one of the five fixed feasibility labels.

    Encodes the decision rules used in the ROB-355 report so the verdict is
    auditable rather than hand-waved:
      - no archive                              -> needs_vendor_data
      - archive but not raw exchange evidence   -> partial
      - raw archive, delisted symbols missing   -> needs_more_data
      - raw archive, delisted covered           -> feasible
    """
    if not s.archive_present:
        return "needs_vendor_data"
    if not s.raw_evidence:
        return "partial"
    if not s.delisted_covered:
        return "needs_more_data"
    return "feasible"


# --------------------------------------------------------------------------- #
# Network helpers (operator RUN only)
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "rob355-audit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:  # noqa: S310
        return r.read()


def list_data_types(granularity: str) -> list[str]:
    """CommonPrefixes (data types) under ``futures/um/<granularity>/``."""
    prefix = f"data/futures/um/{granularity}/"
    xml = _get(S3 + "?" + urllib.parse.urlencode({"delimiter": "/", "prefix": prefix})).decode()
    return sorted(re.findall(r"<Prefix>" + re.escape(prefix) + r"([^/<]+)/</Prefix>", xml))


def list_periods(prefix: str, pat: str) -> list[str]:
    """All period tokens (month/day) matching ``pat`` under ``prefix`` (paginated)."""
    out: list[str] = []
    marker = ""
    while True:
        q: dict[str, str] = {"prefix": prefix}
        if marker:
            q["marker"] = marker
        xml = _get(S3 + "?" + urllib.parse.urlencode(q)).decode()
        toks = re.findall(pat, xml)
        out.extend(toks)
        keys = re.findall(r"<Key>([^<]+)</Key>", xml)
        if "<IsTruncated>true</IsTruncated>" in xml and keys:
            marker = keys[-1]
        else:
            break
    return sorted(set(out))


def sample_columns(url: str) -> tuple[str, int]:
    """Header line and row count of the single CSV inside a sampled archive zip."""
    z = zipfile.ZipFile(io.BytesIO(_get(url)))
    txt = z.read(z.namelist()[0]).decode().splitlines()
    return (txt[0] if txt else ""), max(0, len(txt) - 1)


def probe(symbols: tuple[tuple[str, str | None], ...] = PROBE_SYMBOLS) -> dict:
    """Operator RUN: measure coverage. Returns a summary dict (no raw data)."""
    summary: dict = {
        "source": "data.binance.vision/futures/um",
        "data_types": {g: list_data_types(g) for g in ("monthly", "daily")},
        "funding": {},
        "open_interest": {},
        "samples": {},
    }
    liq_present = any(
        "liquidation" in t.lower() or "forceorder" in t.lower()
        for ts in summary["data_types"].values()
        for t in ts
    )
    summary["liquidation_archive_present"] = liq_present

    for sym, active_to in symbols:
        fr = parse_range(
            list_periods(
                f"data/futures/um/monthly/fundingRate/{sym}/",
                re.escape(sym) + r"-fundingRate-(\d{4}-\d{2})\.zip",
            )
        )
        oi = parse_range(
            list_periods(
                f"data/futures/um/daily/metrics/{sym}/",
                re.escape(sym) + r"-metrics-(\d{4}-\d{2}-\d{2})\.zip",
            )
        )
        summary["funding"][sym] = {
            "range": fr,
            "survivorship_ok": survivorship_ok(fr["last"] if fr else None, active_to),
        }
        summary["open_interest"][sym] = {
            "range": oi,
            "survivorship_ok": survivorship_ok(oi["last"] if oi else None, active_to),
        }

    # one sample each for column/granularity evidence
    btc_oi = list_periods(
        "data/futures/um/daily/metrics/BTCUSDT/",
        re.escape("BTCUSDT") + r"-metrics-(\d{4}-\d{2}-\d{2})\.zip",
    )
    if btc_oi:
        d0 = btc_oi[0]
        hdr, rows = sample_columns(
            f"{BASE}/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d0}.zip"
        )
        summary["samples"]["metrics"] = {"day": d0, "columns": hdr, "rows_in_day": rows}
    btc_fr = list_periods(
        "data/futures/um/monthly/fundingRate/BTCUSDT/",
        re.escape("BTCUSDT") + r"-fundingRate-(\d{4}-\d{2})\.zip",
    )
    if btc_fr:
        m0 = btc_fr[0]
        hdr, rows = sample_columns(
            f"{BASE}/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{m0}.zip"
        )
        summary["samples"]["fundingRate"] = {"month": m0, "columns": hdr, "rows": rows}

    # deterministic verdicts (raw_evidence flags: funding/OI raw; liquidation none here)
    fund_dead_ok = all(
        v["survivorship_ok"] for s, v in summary["funding"].items() if dict(symbols).get(s)
    )
    oi_dead_ok = all(
        v["survivorship_ok"] for s, v in summary["open_interest"].items() if dict(symbols).get(s)
    )
    summary["verdicts"] = {
        "funding_only_baseline": classify_verdict(
            FamilySignals(bool(summary["funding"]), True, fund_dead_ok)
        ),
        "funding_plus_oi": classify_verdict(
            FamilySignals(bool(summary["open_interest"]), True, oi_dead_ok)
        ),
        "liquidation": classify_verdict(FamilySignals(liq_present, True, False)),
        "liquidity_sweep": classify_verdict(
            FamilySignals(True, False, False)  # trade/banded-depth present but not raw L2
        ),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ROB-355 derivatives data feasibility probe (read-only).")
    parser.add_argument(
        "--out",
        action="store_true",
        help="Also write the coverage summary JSON to the gitignored results/ artifact root.",
    )
    args = parser.parse_args(argv)

    s = probe()
    print("data types:", json.dumps(s["data_types"]))
    print("liquidation archive present:", s["liquidation_archive_present"])
    print("\nfunding (monthly) / open_interest (daily metrics) coverage:")
    for sym, _ in PROBE_SYMBOLS:
        fr, oi = s["funding"][sym]["range"], s["open_interest"][sym]["range"]
        frs = f'{fr["first"]}..{fr["last"]} ({fr["count"]}mo)' if fr else "NONE"
        ois = f'{oi["first"]}..{oi["last"]} ({oi["count"]}d)' if oi else "NONE"
        print(
            f'  {sym:14} funding={frs:24} oi={ois:26}'
            f' surv(f/oi)={int(s["funding"][sym]["survivorship_ok"])}/'
            f'{int(s["open_interest"][sym]["survivorship_ok"])}'
        )
    print("\nsamples:", json.dumps(s["samples"]))
    print("verdicts:", json.dumps(s["verdicts"], indent=2))

    if args.out:
        from artifact_paths import resolve_artifact_path

        out = resolve_artifact_path("discovery", "rob355", "derivatives_feasibility.v1.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(s, indent=2))
        print(f"\nsummary written (gitignored): {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
