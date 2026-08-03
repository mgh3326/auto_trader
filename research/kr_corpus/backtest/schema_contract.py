"""Declared schema contract for the KR backtest harness.

SCHEMA_ORIGIN = INFERRED_FROM_LITERALS

This contract is **inferred** from the kr-corpus-v1 brief §3 literals
(START_DATE / CUTOFF / MARKETS / FREQUENCY / PRICE_MODE / TIMEZONE /
SESSION_CALENDAR / partition layout / membership-separate / window splits)
plus the Stage A baseline smoke need for ``trading_value``.

It is **not** reverse-engineered from a real terminal corpus manifest.
When Stage B opens, real manifest + parquet schemas must be contrasted
against this file; any mismatch is a **loud fail** with a difference list.
Silent column inference / coercion is forbidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa

__all__ = [
    "CONTRACT_PATH",
    "SCHEMA_ORIGIN",
    "SchemaContractError",
    "SchemaMismatchError",
    "arrow_schema_for",
    "load_contract",
    "validate_table_schema",
]

SCHEMA_ORIGIN = "INFERRED_FROM_LITERALS"
CONTRACT_PATH = Path(__file__).resolve().parent / "schema_contract.v1.json"

_ARROW_DTYPE_MAP: dict[str, pa.DataType] = {
    "string": pa.string(),
    "float64": pa.float64(),
    "bool": pa.bool_(),
}


class SchemaContractError(RuntimeError):
    """Base for schema-contract failures."""


class SchemaMismatchError(SchemaContractError):
    """Parquet/table schema does not exactly match the declared contract."""


def load_contract() -> dict[str, Any]:
    """Load the declared JSON contract (source of truth for column/dtype)."""
    raw = CONTRACT_PATH.read_text(encoding="utf-8")
    contract = json.loads(raw)
    if contract.get("schema_origin") != SCHEMA_ORIGIN:
        raise SchemaContractError(
            f"schema_origin must be {SCHEMA_ORIGIN!r}, "
            f"got {contract.get('schema_origin')!r}"
        )
    return contract


def arrow_schema_for(dataset: str) -> pa.Schema:
    """Exact Arrow schema pinned by the declared contract for ``dataset``."""
    contract = load_contract()
    try:
        ds = contract["datasets"][dataset]
    except KeyError as exc:
        raise SchemaContractError(f"unknown dataset {dataset!r}") from exc
    fields: list[pa.Field] = []
    dtypes: dict[str, str] = ds["column_dtypes"]
    for name in ds["required_columns"]:
        dtype_name = dtypes[name]
        try:
            arrow_type = _ARROW_DTYPE_MAP[dtype_name]
        except KeyError as exc:
            raise SchemaContractError(
                f"unsupported dtype {dtype_name!r} for column {name!r}"
            ) from exc
        fields.append(pa.field(name, arrow_type, nullable=False))
    return pa.schema(fields)


def validate_table_schema(table: pa.Table, dataset: str) -> None:
    """Refuse unless ``table.schema`` exactly matches the contract schema.

    No silent column drop, rename, or dtype coercion.
    """
    expected = arrow_schema_for(dataset)
    actual = table.schema
    # Exact name+type match; ignore metadata only.
    if not actual.equals(expected, check_metadata=False):
        diffs = _schema_diff(expected, actual)
        raise SchemaMismatchError(f"schema mismatch for dataset={dataset!r}: {diffs}")


def _schema_diff(expected: pa.Schema, actual: pa.Schema) -> list[str]:
    diffs: list[str] = []
    exp_names = list(expected.names)
    act_names = list(actual.names)
    if exp_names != act_names:
        diffs.append(f"columns expected={exp_names} actual={act_names}")
    for name in exp_names:
        if name not in act_names:
            diffs.append(f"missing column {name!r}")
            continue
        exp_type = expected.field(name).type
        act_type = actual.field(name).type
        if exp_type != act_type:
            diffs.append(f"dtype {name!r}: expected={exp_type} actual={act_type}")
    for name in act_names:
        if name not in exp_names:
            diffs.append(f"unexpected column {name!r}")
    return diffs
