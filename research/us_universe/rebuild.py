"""Manual command: rebuild the US PIT listing universe from public archives.

    # fetch any new Internet Archive captures into the cache, then build
    uv run python -m research.us_universe.rebuild --root <ARTIFACT_ROOT> --out <DIR>
    # rebuild from the cache only (reproducibility check: same bytes out)
    uv run python -m research.us_universe.rebuild --root <ARTIFACT_ROOT> --out <DIR> --offline

Inputs (all public, unauthenticated):

* Internet Archive (Wayback Machine) captures of the NASDAQ Trader symbol
  directory files, 2008→today, cached write-once under
  ``<root>/sources/wayback/<series>/<capture-timestamp>.bin``;
* this package's own daily snapshots under ``<root>/snapshots/``.

Outputs, written once into ``--out`` (refuses to overwrite):

* ``us_pit_universe.csv`` — one row per listing interval (see ``pit.py``);
* ``manifest.json``       — every input with its sha256 and acceptance, per-
  series coverage, inactive shares, output digest, generating commit;
* ``coverage.md``         — the same coverage and gaps, human-readable.

The outputs are a pure function of the cached inputs and the code, so an
``--offline`` rebuild of the same cache reproduces them byte for byte.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .pit import (
    PITRow,
    build_pit_rows,
    inactive_shares,
    rows_to_csv,
    sha256_hex,
)
from .snapshot import REPO_ROOT
from .store import Fetcher, code_commit, http_fetcher, write_once
from .symdir import (
    SERIES,
    SOURCE_URLS,
    Observation,
    SymbolDirectoryError,
    decode_body,
    parse_symbol_directory,
)

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
# us-corpus-v1 window start; the inactive share is also reported for it.
CORPUS_WINDOW_START = date(2016, 1, 1)
# ROB-1431 brief: inactive share of the current (survivor-only) US corpus.
CURRENT_CORPUS_INACTIVE_SHARE = 0.006
OUTPUT_CSV = "us_pit_universe.csv"
OUTPUT_MANIFEST = "manifest.json"
OUTPUT_COVERAGE = "coverage.md"


@dataclass(frozen=True)
class InputFile:
    source: str  # wayback | local_snapshot
    series: str
    ref: str  # capture timestamp or snapshot day
    path: Path
    url: str


def cdx_url(series: str) -> str:
    query = urllib.parse.urlencode(
        {
            "url": f"nasdaqtrader.com/dynamic/SymDir/{series}.txt",
            "output": "json",
            "fl": "timestamp,original,statuscode,digest",
            "filter": "statuscode:200",
            "collapse": "digest",
        }
    )
    return f"{CDX_ENDPOINT}?{query}"


def capture_url(timestamp: str, original: str) -> str:
    # ``id_`` asks the archive for the original bytes without its HTML wrapper.
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


def parse_cdx(body: bytes) -> list[tuple[str, str]]:
    rows = json.loads(body.decode("utf-8")) if body.strip() else []
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    ti, oi = header.index("timestamp"), header.index("original")
    return sorted({(r[ti], r[oi]) for r in data})


def wayback_cache_path(root: Path, series: str, timestamp: str) -> Path:
    return root / "sources" / "wayback" / series / f"{timestamp}.bin"


def refresh_wayback_cache(root: Path, fetch: Fetcher) -> list[str]:
    """Fetch captures missing from the cache. Returns human-readable failures."""
    failures: list[str] = []
    for series in SERIES:
        try:
            captures = parse_cdx(fetch(cdx_url(series)))
        except Exception as exc:  # noqa: BLE001 — recorded as a coverage gap
            failures.append(f"{series}: CDX listing failed: {type(exc).__name__}")
            continue
        for timestamp, original in captures:
            path = wayback_cache_path(root, series, timestamp)
            if path.exists():
                continue
            try:
                write_once(path, fetch(capture_url(timestamp, original)))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{series}@{timestamp}: {type(exc).__name__}")
    return failures


def discover_inputs(root: Path) -> list[InputFile]:
    found: list[InputFile] = []
    for series in SERIES:
        for path in sorted((root / "sources" / "wayback" / series).glob("*.bin")):
            ts = path.stem
            found.append(
                InputFile(
                    "wayback", series, ts, path, capture_url(ts, SOURCE_URLS[series])
                )
            )
        for path in sorted((root / "snapshots").glob(f"*/{series}.txt")):
            found.append(
                InputFile(
                    "local_snapshot",
                    series,
                    path.parent.name,
                    path,
                    SOURCE_URLS[series],
                )
            )
    return found


def load_observations(
    inputs: list[InputFile],
) -> tuple[list[Observation], list[dict]]:
    observations: list[Observation] = []
    records: list[dict] = []
    for item in inputs:
        raw = item.path.read_bytes()
        rec = {
            "source": item.source,
            "series": item.series,
            "ref": item.ref,
            "url": item.url,
            "sha256": sha256_hex(raw),
            "bytes": len(raw),
        }
        try:
            obs = parse_symbol_directory(item.series, decode_body(raw))
        except (SymbolDirectoryError, UnicodeDecodeError, OSError, EOFError) as exc:
            rec.update(accepted=False, reason=f"{type(exc).__name__}: {exc}"[:200])
        else:
            observations.append(obs)
            rec.update(
                accepted=True,
                as_of=obs.as_of.isoformat(),
                file_created_at=obs.created_at,
                rows=len(obs.rows),
            )
        records.append(rec)
    records.sort(key=lambda r: (r["series"], r["source"], r["ref"]))
    return observations, records


def series_coverage(observations: list[Observation]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for series in SERIES:
        days = sorted({o.as_of for o in observations if o.series == series})
        gaps = [(a, b, (b - a).days) for a, b in zip(days, days[1:], strict=False)]
        long_gaps = [g for g in gaps if g[2] > 365]
        out[series] = {
            "observation_days": len(days),
            "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None,
            "max_gap_days": max((g[2] for g in gaps), default=0),
            "median_gap_days": sorted(g[2] for g in gaps)[len(gaps) // 2]
            if gaps
            else 0,
            "gaps_over_365_days": [
                {"from": a.isoformat(), "to": b.isoformat(), "days": d}
                for a, b, d in long_gaps
            ],
            "observations_per_year": {
                str(y): sum(1 for d in days if d.year == y)
                for y in sorted({d.year for d in days})
            },
        }
    return out


def build_manifest(
    rows: list[PITRow],
    observations: list[Observation],
    input_records: list[dict],
    csv_bytes: bytes,
    commit: str,
) -> dict:
    window_end = max(o.as_of for o in observations)
    shares = inactive_shares(
        rows, window_start=CORPUS_WINDOW_START, window_end=window_end
    )
    return {
        "schema": "us_pit_universe_manifest.v1",
        "issue": "ROB-1431",
        "code_commit": commit,
        "outputs": {
            OUTPUT_CSV: {
                "sha256": sha256_hex(csv_bytes),
                "bytes": len(csv_bytes),
                "rows": len(rows),
                "symbols": len({r.symbol for r in rows}),
            }
        },
        "as_of_latest_observation": window_end.isoformat(),
        "inactive_share": [s.as_dict() for s in shares],
        "current_corpus_reference": {
            "inactive_share": CURRENT_CORPUS_INACTIVE_SHARE,
            "source": "ROB-1431 brief (survivor-only us-corpus-v1); not re-measured here",
        },
        "series_coverage": series_coverage(observations),
        "inputs": {
            "accepted": sum(1 for r in input_records if r["accepted"]),
            "rejected": sum(1 for r in input_records if not r["accepted"]),
            "files": input_records,
        },
    }


def render_coverage(manifest: dict) -> str:
    lines = [
        "# US PIT universe — coverage and gaps (generated)",
        "",
        f"- code commit: `{manifest['code_commit']}`",
        f"- latest observation: {manifest['as_of_latest_observation']}",
        f"- `{OUTPUT_CSV}` sha256 `{manifest['outputs'][OUTPUT_CSV]['sha256']}`, "
        f"{manifest['outputs'][OUTPUT_CSV]['rows']} intervals, "
        f"{manifest['outputs'][OUTPUT_CSV]['symbols']} symbols",
        f"- inputs: {manifest['inputs']['accepted']} accepted, "
        f"{manifest['inputs']['rejected']} rejected",
        "",
        "## Inactive share",
        "",
        "| scope | symbols | inactive | share |",
        "|---|---:|---:|---:|",
    ]
    for s in manifest["inactive_share"]:
        lines.append(
            f"| {s['scope']} | {s['symbols']} | {s['inactive']} | "
            f"{s['inactive_share'] * 100:.2f}% |"
        )
    ref = manifest["current_corpus_reference"]
    lines += [
        f"| current corpus (reference: {ref['source']}) | | | "
        f"{ref['inactive_share'] * 100:.2f}% |",
        "",
        "## Series coverage",
        "",
    ]
    for series, cov in manifest["series_coverage"].items():
        lines += [
            f"### {series}",
            "",
            f"- {cov['observation_days']} observation days, {cov['first']} → {cov['last']}",
            f"- gap between observations: median {cov['median_gap_days']} days, "
            f"max {cov['max_gap_days']} days",
            "- per year: "
            + ", ".join(f"{y}: {n}" for y, n in cov["observations_per_year"].items()),
        ]
        if cov["gaps_over_365_days"]:
            lines.append("- gaps over 365 days (event dates inside are unresolved):")
            lines += [
                f"  - {g['from']} → {g['to']} ({g['days']} days)"
                for g in cov["gaps_over_365_days"]
            ]
        lines.append("")
    rejected = [r for r in manifest["inputs"]["files"] if not r["accepted"]]
    if rejected:
        lines += ["## Rejected inputs", ""]
        lines += [
            f"- {r['series']} {r['source']} {r['ref']}: {r['reason']}" for r in rejected
        ]
        lines.append("")
    return "\n".join(lines)


def rebuild(root: Path, out: Path, *, commit: str) -> dict:
    observations, records = load_observations(discover_inputs(root))
    if not observations:
        raise SystemExit("no accepted observations under --root; nothing to build")
    rows = build_pit_rows(observations)
    csv_bytes = rows_to_csv(rows)
    manifest = build_manifest(rows, observations, records, csv_bytes, commit)
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    write_once(out / OUTPUT_CSV, csv_bytes)
    write_once(out / OUTPUT_MANIFEST, manifest_bytes)
    write_once(out / OUTPUT_COVERAGE, render_coverage(manifest).encode())
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True, help="artifact root")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--offline", action="store_true", help="build from the cache only"
    )
    args = parser.parse_args(argv)
    if not args.offline:
        for failure in refresh_wayback_cache(args.root, http_fetcher()):
            print(f"fetch gap: {failure}", file=sys.stderr)
    manifest = rebuild(args.root, args.out, commit=code_commit(REPO_ROOT))
    for share in manifest["inactive_share"]:
        print(
            f"{share['scope']}: {share['inactive']}/{share['symbols']} "
            f"= {share['inactive_share'] * 100:.2f}% inactive"
        )
    print(f"{OUTPUT_CSV} sha256 {manifest['outputs'][OUTPUT_CSV]['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
