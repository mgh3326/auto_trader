"""Build a main-scope clamp-admit view without touching raw snapshots.

The immutable valid-bar snapshot remains authoritative. This offline builder
reads only the main snapshot and writes a separately checksummed sidecar view:

* source-valid bars are copied with clamped=false;
* tradeable adjusted-price OHLC rounding anomalies are admitted after applying
  the signed clamp formula; and
* no-trade open=high=low=0 anomalies are classified and retained outside the
  view, never admitted.

The builder refuses a holdout source root. It has no network code and never
opens a final holdout path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ClampAdmitBuildError(RuntimeError):
    """The sidecar cannot be built safely from the supplied source snapshot."""


@dataclass(frozen=True)
class DerivedFile:
    relative_path: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class ClampAdmitResult:
    derived_root: Path
    source_valid_bar_rows: int
    clamp_rows_admitted: int
    no_trade_rows_excluded: int
    clamp_admit_rows: int
    checksum_list_sha256: str


Partition = tuple[str, int, str]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> DerivedFile:
    if path.exists():
        raise ClampAdmitBuildError(f"refusing to overwrite derived file: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.partial")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != len(data):
            raise ClampAdmitBuildError("derived atomic write length mismatch")
        os.replace(temporary, path)
    except Exception:
        # Preserve a failed .partial for inspection; never delete by default.
        raise
    return DerivedFile(
        relative_path=str(path),
        sha256=_sha256(data),
        byte_size=len(data),
    )


def _parquet_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("session", pa.string(), nullable=False),
            pa.field("market", pa.string(), nullable=False),
            pa.field("ticker", pa.string(), nullable=False),
            pa.field("open", pa.int64(), nullable=False),
            pa.field("high", pa.int64(), nullable=False),
            pa.field("low", pa.int64(), nullable=False),
            pa.field("close", pa.int64(), nullable=False),
            pa.field("volume", pa.int64(), nullable=False),
            pa.field("value", pa.int64(), nullable=True),
            pa.field("price_mode", pa.string(), nullable=False),
            pa.field("source_product", pa.string(), nullable=False),
            pa.field("source_high", pa.int64(), nullable=False),
            pa.field("source_low", pa.int64(), nullable=False),
            pa.field("clamped", pa.bool_(), nullable=False),
            pa.field("clamp_delta_high", pa.int64(), nullable=False),
            pa.field("clamp_delta_low", pa.int64(), nullable=False),
            pa.field("clamp_delta_high_relative", pa.float64(), nullable=True),
            pa.field("clamp_delta_low_relative", pa.float64(), nullable=True),
            pa.field("clamp_classification", pa.string(), nullable=False),
            pa.field("admitted", pa.bool_(), nullable=False),
        ]
    )


def _parquet_bytes(rows: list[dict[str, object]]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    sink = pa.BufferOutputStream()
    table = pa.Table.from_pylist(rows, schema=_parquet_schema())
    pq.write_table(table, sink, compression="zstd", version="2.6")
    return sink.getvalue().to_pybytes()


def _write_parquet(
    partial_root: Path, relative_path: str, rows: list[dict[str, object]]
) -> DerivedFile:
    record = _atomic_write(partial_root / relative_path, _parquet_bytes(rows))
    return DerivedFile(
        relative_path=relative_path,
        sha256=record.sha256,
        byte_size=record.byte_size,
    )


def _relative_delta(delta: int, source_value: int) -> float | None:
    if source_value == 0:
        return None
    return delta / abs(source_value)


def _source_valid_row(row: dict[str, object]) -> dict[str, object]:
    source_high = int(row["high"])
    source_low = int(row["low"])
    return {
        "session": str(row["session"]),
        "market": str(row["market"]),
        "ticker": str(row["ticker"]),
        "open": int(row["open"]),
        "high": source_high,
        "low": source_low,
        "close": int(row["close"]),
        "volume": int(row["volume"]),
        "value": row["value"],
        "price_mode": str(row["price_mode"]),
        "source_product": str(row["source_product"]),
        "source_high": source_high,
        "source_low": source_low,
        "clamped": False,
        "clamp_delta_high": 0,
        "clamp_delta_low": 0,
        "clamp_delta_high_relative": 0.0,
        "clamp_delta_low_relative": 0.0,
        "clamp_classification": "source_valid",
        "admitted": True,
    }


def _anomaly_row(
    *,
    session: str,
    market: str,
    ticker: str,
    detail: dict[str, object],
    classification: str,
    admitted: bool,
) -> dict[str, object]:
    open_price = int(detail["open"])
    source_high = int(detail["high"])
    source_low = int(detail["low"])
    close_price = int(detail["close"])
    if classification == "no_trade":
        clamped_high = source_high
        clamped_low = source_low
        delta_high = 0
        delta_low = 0
        if not (
            open_price == 0 and source_high == 0 and source_low == 0 and close_price > 0
        ):
            raise ClampAdmitBuildError("no_trade classification did not match OHLC")
        if admitted or delta_high or delta_low:
            raise ClampAdmitBuildError("no_trade row must not be clamped or admitted")
    else:
        clamped_high = max(source_high, open_price, close_price)
        clamped_low = min(source_low, open_price, close_price)
        delta_high = clamped_high - source_high
        delta_low = source_low - clamped_low
        if not admitted:
            raise ClampAdmitBuildError("only no_trade rows may be excluded")
        if not (
            clamped_low <= min(open_price, close_price)
            and max(open_price, close_price) <= clamped_high
            and (delta_high > 0 or delta_low > 0)
        ):
            raise ClampAdmitBuildError(
                "tradeable clamp row did not repair OHLC invariant"
            )

    return {
        "session": session,
        "market": market,
        "ticker": ticker,
        "open": open_price,
        "high": clamped_high,
        "low": clamped_low,
        "close": close_price,
        "volume": int(detail["volume"]),
        "value": detail.get("value"),
        "price_mode": "adjusted",
        "source_product": "pykrx",
        "source_high": source_high,
        "source_low": source_low,
        "clamped": admitted,
        "clamp_delta_high": delta_high,
        "clamp_delta_low": delta_low,
        "clamp_delta_high_relative": _relative_delta(delta_high, source_high),
        "clamp_delta_low_relative": _relative_delta(delta_low, source_low),
        "clamp_classification": classification,
        "admitted": admitted,
    }


def _require_main_source(source_root: Path) -> dict[str, object]:
    if "holdout" in source_root.parts:
        raise ClampAdmitBuildError("holdout source roots are forbidden")
    manifest_path = source_root / "manifest.json"
    if not manifest_path.is_file():
        raise ClampAdmitBuildError("source main manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("scope") != "main":
        raise ClampAdmitBuildError("clamp-admit accepts only a main-scope snapshot")
    required_paths = (
        source_root / "dataset",
        source_root / "gaps",
        source_root / "source-anomalies.jsonl",
    )
    if not all(path.exists() for path in required_paths):
        raise ClampAdmitBuildError("source main snapshot lacks required artifacts")
    return manifest


def _gap_market_by_key(source_root: Path) -> dict[tuple[str, str], str]:
    import pyarrow.parquet as pq

    result: dict[tuple[str, str], str] = {}
    paths = sorted((source_root / "gaps").glob("market=*/year=*/missing.parquet"))
    if not paths:
        raise ClampAdmitBuildError("source main snapshot has no gap partitions")
    for path in paths:
        rows = (
            pq.ParquetFile(path)
            .read(columns=["session", "market", "ticker", "reason"])
            .to_pylist()
        )
        for row in rows:
            if row["reason"] != "ohlc_invariant_violation":
                continue
            key = (str(row["session"]), str(row["ticker"]))
            market = str(row["market"])
            previous = result.setdefault(key, market)
            if previous != market:
                raise ClampAdmitBuildError(
                    "one anomaly key maps to more than one market"
                )
    return result


def _classify_anomalies(
    source_root: Path,
) -> tuple[
    dict[Partition, list[dict[str, object]]],
    dict[tuple[str, int], list[dict[str, object]]],
    int,
    int,
]:
    market_by_key = _gap_market_by_key(source_root)
    clamp_rows: dict[Partition, list[dict[str, object]]] = defaultdict(list)
    no_trade_rows: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    seen_keys: set[tuple[str, str]] = set()
    clamp_count = 0
    no_trade_count = 0

    with (source_root / "source-anomalies.jsonl").open("r", encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("kind") != "ohlc_invariant_violation":
                continue
            session = str(record["session"])
            ticker = str(record["ticker"])
            key = (session, ticker)
            if key in seen_keys:
                raise ClampAdmitBuildError("duplicate source anomaly key")
            seen_keys.add(key)
            market = market_by_key.get(key)
            if market is None:
                raise ClampAdmitBuildError("OHLC anomaly has no matching main gap")
            detail = record["detail"]
            if not isinstance(detail, dict):
                raise ClampAdmitBuildError("OHLC anomaly detail is not an object")
            year = int(session[:4])
            is_no_trade = (
                int(detail["open"]) == 0
                and int(detail["high"]) == 0
                and int(detail["low"]) == 0
                and int(detail["close"]) > 0
            )
            if is_no_trade:
                no_trade_rows[(market, year)].append(
                    _anomaly_row(
                        session=session,
                        market=market,
                        ticker=ticker,
                        detail=detail,
                        classification="no_trade",
                        admitted=False,
                    )
                )
                no_trade_count += 1
                continue

            clamp_rows[(market, year, ticker)].append(
                _anomaly_row(
                    session=session,
                    market=market,
                    ticker=ticker,
                    detail=detail,
                    classification="tradeable_adjusted_rounding",
                    admitted=True,
                )
            )
            clamp_count += 1

    if seen_keys != set(market_by_key):
        raise ClampAdmitBuildError(
            "main OHLC anomaly keys and ohlc_invariant gap keys differ"
        )
    return clamp_rows, no_trade_rows, clamp_count, no_trade_count


def _partition_from_path(source_root: Path, path: Path) -> Partition:
    relative = path.relative_to(source_root / "dataset")
    market_part, year_part, ticker_part = relative.parts
    if not (
        market_part.startswith("market=")
        and year_part.startswith("year=")
        and ticker_part.startswith("ticker=")
        and ticker_part.endswith(".parquet")
    ):
        raise ClampAdmitBuildError("unexpected source dataset partition path")
    return (
        market_part.removeprefix("market="),
        int(year_part.removeprefix("year=")),
        ticker_part.removeprefix("ticker=").removesuffix(".parquet"),
    )


def _relative_dataset_path(partition: Partition) -> str:
    market, year, ticker = partition
    return f"dataset/market={market}/year={year}/ticker={ticker}.parquet"


def _render_readme(result: ClampAdmitResult) -> str:
    return f"""# Clamp-admit derived view

This sidecar is a main-scope-only derivative of the immutable kr-corpus-v1
valid-bar snapshot. It does not overwrite, delete, or modify the source
valid-bar view. It contains {result.source_valid_bar_rows} copied valid bars
plus {result.clamp_rows_admitted} separately admitted tradeable rounding rows,
for {result.clamp_admit_rows} rows in total.

The separate no_trade/ dataset contains {result.no_trade_rows_excluded} rows
where open=high=low=0 and close>0. Those rows are classified no_trade and are
not admitted into dataset/.

Every dataset/ row has clamped, clamp_delta_high, clamp_delta_low,
clamp_delta_high_relative, clamp_delta_low_relative, source_high, source_low,
clamp_classification, and admitted columns. clamp_delta_high is
clamped_high - source_high; clamp_delta_low is source_low - clamped_low. Both
are non-negative magnitudes. Consumers can filter clamped rows or set a
delta threshold without losing the original valid-bar view.

The clamp formula is high=max(source_high, open, close) and
low=min(source_low, open, close). It can create a high/low value that was not
an observed transaction or remove an apparent trigger. OHLC trigger logic
(stops, limit fills, and breakouts) can therefore still be distorted. Treat
clamped rows as an explicit sensitivity dimension, not as ground truth.
"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_clamp_admit_view(derived_root: Path) -> dict[str, int | bool]:
    """Recompute sidecar checksums and row-level clamp invariants.

    The verifier accepts only a derived sidecar root. It deliberately has no
    source-root or holdout argument, so running it cannot inspect holdout data.
    """
    import pyarrow.parquet as pq

    derived_root = derived_root.resolve()
    if "holdout" in derived_root.parts:
        raise ClampAdmitBuildError("holdout derived roots are forbidden")
    manifest_path = derived_root / "manifest.json"
    checksum_path = derived_root / "checksums.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise ClampAdmitBuildError("derived view manifest or checksum list is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("scope") != "main_only":
        raise ClampAdmitBuildError("derived view is not main-only")
    if manifest.get("checksums_sha256") != _file_sha256(checksum_path):
        raise ClampAdmitBuildError("derived checksum-list hash does not match manifest")

    checksum_rows = [
        line.split("  ", 1)
        for line in checksum_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not checksum_rows or any(len(row) != 2 for row in checksum_rows):
        raise ClampAdmitBuildError("derived checksum list is malformed")
    listed_paths = {relative_path for _expected, relative_path in checksum_rows}
    actual_paths = {
        str(path.relative_to(derived_root))
        for path in derived_root.rglob("*")
        if path.is_file()
    }
    if actual_paths != listed_paths | {"checksums.sha256", "manifest.json"}:
        raise ClampAdmitBuildError("derived files and checksum registry differ")
    for expected, relative_path in checksum_rows:
        candidate = derived_root / relative_path
        if not candidate.is_file() or _file_sha256(candidate) != expected:
            raise ClampAdmitBuildError("derived checksum verification failed")

    clamp_rows = 0
    source_valid_rows = 0
    for path in sorted(
        (derived_root / "dataset").glob("market=*/year=*/ticker=*.parquet")
    ):
        rows = pq.ParquetFile(path).read().to_pylist()
        for row in rows:
            high = int(row["high"])
            low = int(row["low"])
            open_price = int(row["open"])
            close_price = int(row["close"])
            if not (
                low <= min(open_price, close_price)
                and max(open_price, close_price) <= high
                and row["admitted"] is True
            ):
                raise ClampAdmitBuildError("dataset row violates clamp-admit contract")
            delta_high = int(row["clamp_delta_high"])
            delta_low = int(row["clamp_delta_low"])
            if delta_high < 0 or delta_low < 0:
                raise ClampAdmitBuildError("clamp deltas must be non-negative")
            if row["clamped"]:
                if (
                    high != int(row["source_high"]) + delta_high
                    or low != int(row["source_low"]) - delta_low
                    or row["clamp_classification"] != "tradeable_adjusted_rounding"
                    or (delta_high == 0 and delta_low == 0)
                ):
                    raise ClampAdmitBuildError("clamped row does not preserve deltas")
                clamp_rows += 1
            else:
                if (
                    high != int(row["source_high"])
                    or low != int(row["source_low"])
                    or delta_high != 0
                    or delta_low != 0
                    or row["clamp_classification"] != "source_valid"
                ):
                    raise ClampAdmitBuildError("source-valid row was not copied intact")
                source_valid_rows += 1

    no_trade_rows = 0
    for path in sorted(
        (derived_root / "no_trade").glob("market=*/year=*/excluded.parquet")
    ):
        for row in pq.ParquetFile(path).read().to_pylist():
            if not (
                row["clamp_classification"] == "no_trade"
                and row["clamped"] is False
                and row["admitted"] is False
                and int(row["open"]) == 0
                and int(row["high"]) == 0
                and int(row["low"]) == 0
                and int(row["close"]) > 0
                and int(row["clamp_delta_high"]) == 0
                and int(row["clamp_delta_low"]) == 0
            ):
                raise ClampAdmitBuildError("no_trade row was incorrectly admitted")
            no_trade_rows += 1

    expected = {
        "source_valid_bar_rows": source_valid_rows,
        "clamp_rows_admitted": clamp_rows,
        "no_trade_rows_excluded_from_clamp": no_trade_rows,
        "clamp_admit_rows": source_valid_rows + clamp_rows,
    }
    for field_name, actual in expected.items():
        if manifest.get(field_name) != actual:
            raise ClampAdmitBuildError(
                f"derived manifest count mismatch for {field_name}"
            )
    return {
        "checksums_verified": True,
        "source_valid_bar_rows": source_valid_rows,
        "clamp_rows_admitted": clamp_rows,
        "no_trade_rows_excluded": no_trade_rows,
        "clamp_admit_rows": source_valid_rows + clamp_rows,
    }


def build_clamp_admit_view(
    source_main_root: Path, derived_root: Path
) -> ClampAdmitResult:
    """Create a separately checksummed main-only clamp-admit sidecar atomically."""
    source_main_root = source_main_root.resolve()
    derived_root = derived_root.resolve()
    source_manifest = _require_main_source(source_main_root)
    if derived_root.exists():
        raise ClampAdmitBuildError("final clamp-admit view already exists")
    partial_root = derived_root.with_name(f"{derived_root.name}.partial")
    if partial_root.exists():
        raise ClampAdmitBuildError("clamp-admit .partial already exists")
    partial_root.mkdir(parents=True, exist_ok=False)

    records: list[DerivedFile] = []
    try:
        clamp_rows, no_trade_rows, clamp_count, no_trade_count = _classify_anomalies(
            source_main_root
        )

        import pyarrow.parquet as pq

        source_valid_bar_rows = 0
        source_paths = sorted(
            (source_main_root / "dataset").glob("market=*/year=*/ticker=*.parquet")
        )
        if not source_paths:
            raise ClampAdmitBuildError("source main snapshot has no valid-bar files")
        for source_path in source_paths:
            partition = _partition_from_path(source_main_root, source_path)
            rows = [
                _source_valid_row(row)
                for row in pq.ParquetFile(source_path).read().to_pylist()
            ]
            source_valid_bar_rows += len(rows)
            rows.extend(clamp_rows.pop(partition, []))
            rows.sort(key=lambda row: (str(row["session"]), str(row["ticker"])))
            records.append(
                _write_parquet(
                    partial_root,
                    _relative_dataset_path(partition),
                    rows,
                )
            )

        for partition, rows in sorted(clamp_rows.items()):
            rows.sort(key=lambda row: (str(row["session"]), str(row["ticker"])))
            records.append(
                _write_parquet(
                    partial_root,
                    _relative_dataset_path(partition),
                    rows,
                )
            )

        for (market, year), rows in sorted(no_trade_rows.items()):
            rows.sort(key=lambda row: (str(row["session"]), str(row["ticker"])))
            records.append(
                _write_parquet(
                    partial_root,
                    f"no_trade/market={market}/year={year}/excluded.parquet",
                    rows,
                )
            )

        clamp_admit_rows = source_valid_bar_rows + clamp_count
        result_without_checksum = ClampAdmitResult(
            derived_root=derived_root,
            source_valid_bar_rows=source_valid_bar_rows,
            clamp_rows_admitted=clamp_count,
            no_trade_rows_excluded=no_trade_count,
            clamp_admit_rows=clamp_admit_rows,
            checksum_list_sha256="",
        )
        readme_record = _atomic_write(
            partial_root / "README.md",
            _render_readme(result_without_checksum).encode("utf-8"),
        )
        records.append(
            DerivedFile("README.md", readme_record.sha256, readme_record.byte_size)
        )

        checksum_lines = "".join(
            f"{record.sha256}  {record.relative_path}\n"
            for record in sorted(records, key=lambda record: record.relative_path)
        )
        checksum_record = _atomic_write(
            partial_root / "checksums.sha256", checksum_lines.encode("utf-8")
        )
        checksum_hash = checksum_record.sha256
        result = ClampAdmitResult(
            derived_root=derived_root,
            source_valid_bar_rows=source_valid_bar_rows,
            clamp_rows_admitted=clamp_count,
            no_trade_rows_excluded=no_trade_count,
            clamp_admit_rows=clamp_admit_rows,
            checksum_list_sha256=checksum_hash,
        )
        manifest = {
            "view_id": "kr-corpus-v1-clamp-admit-v1",
            "scope": "main_only",
            "source_corpus_id": source_manifest["corpus_id"],
            "source_run_id": source_main_root.name,
            "source_manifest_sha256": _sha256(
                (source_main_root / "manifest.json").read_bytes()
            ),
            "source_valid_bar_view_unchanged": True,
            "source_valid_bar_rows": source_valid_bar_rows,
            "clamp_rows_admitted": clamp_count,
            "no_trade_rows_excluded_from_clamp": no_trade_count,
            "clamp_admit_rows": clamp_admit_rows,
            "row_contract": {
                "clamped_column": "data_column",
                "clamp_delta_high": "clamped_high_minus_source_high",
                "clamp_delta_low": "source_low_minus_clamped_low",
                "clamp_formula": {
                    "high": "max(source_high, open, close)",
                    "low": "min(source_low, open, close)",
                },
                "no_trade_classification": "open=high=low=0 and close>0",
                "no_trade_admitted": False,
                "fill_distortion_warning": (
                    "clamped high/low can create or remove OHLC trigger events; "
                    "filter clamped rows for sensitivity analysis"
                ),
            },
            "checksums_sha256": checksum_hash,
            "files_list_location": "checksums.sha256",
        }
        _atomic_write(partial_root / "manifest.json", _canonical_json(manifest))
        os.replace(partial_root, derived_root)
        return result
    except Exception:
        # Leave the complete .partial tree behind for operator inspection.
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a main-only, separately checksummed clamp-admit view."
    )
    parser.add_argument("--source-main-root", type=Path, required=True)
    parser.add_argument("--derived-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = build_clamp_admit_view(args.source_main_root, args.derived_root)
    print(
        json.dumps(
            {
                "derived_root": str(result.derived_root),
                "source_valid_bar_rows": result.source_valid_bar_rows,
                "clamp_rows_admitted": result.clamp_rows_admitted,
                "no_trade_rows_excluded": result.no_trade_rows_excluded,
                "clamp_admit_rows": result.clamp_admit_rows,
                "checksums_sha256": result.checksum_list_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
