"""Declared schema contract for the KR backtest harness and market adapters.

SCHEMA_ORIGIN = SEALED_CORPUS_V1

Contracts declare the **sealed corpus** column names and types after contrast
against real parquet. Adapters own an explicit mapping layer from those
columns onto harness bar objects. Silent rename/coercion outside the declared
``harness_column_map`` is forbidden.
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
    "ContractTablePolicyError",
    "arrow_schema_for",
    "date_column_for",
    "enforce_table_load_policy",
    "load_contract",
    "types_compatible",
    "validate_table_schema",
]

SCHEMA_ORIGIN = "SEALED_CORPUS_V1"
CONTRACT_PATH = Path(__file__).resolve().parent / "schema_contract.v1.json"

_ARROW_DTYPE_MAP: dict[str, pa.DataType] = {
    "string": pa.string(),
    "large_string": pa.large_string(),
    "float64": pa.float64(),
    "int64": pa.int64(),
    "bool": pa.bool_(),
    "timestamp_ms": pa.timestamp("ms"),
    "timestamp_ms_utc": pa.timestamp("ms", tz="UTC"),
}


class SchemaContractError(RuntimeError):
    """Base for schema-contract failures."""


class SchemaMismatchError(SchemaContractError):
    """Parquet/table schema does not match the declared contract."""


class ContractTablePolicyError(SchemaContractError):
    """Contract-declared post-schema table policy refused the load.

    Raised for required column *values* (e.g. frequency must be ``1d``) and
    other structural table policies that are not column-dtype mismatches.
    Label/metadata refusals from venue-specific policy modules propagate as
    their own exception types (e.g. ``UnlabeledParquetError``).
    """


def load_contract(contract_path: Path | str | None = None) -> dict[str, Any]:
    """Load a declared local JSON contract (source of truth for schema).

    ``CONTRACT_PATH`` remains the KR default. Market adapters pass a committed
    sealed-corpus contract explicitly; this loader never discovers a schema
    from a live corpus artifact at import time.
    """
    path = Path(contract_path) if contract_path is not None else CONTRACT_PATH
    raw = path.read_text(encoding="utf-8")
    contract = json.loads(raw)
    if contract.get("schema_origin") != SCHEMA_ORIGIN:
        raise SchemaContractError(
            f"schema_origin must be {SCHEMA_ORIGIN!r}, "
            f"got {contract.get('schema_origin')!r}"
        )
    return contract


def _dataset_spec(
    dataset: str, *, contract_path: Path | str | None = None
) -> dict[str, Any]:
    contract = load_contract(contract_path)
    try:
        return contract["datasets"][dataset]
    except KeyError as exc:
        raise SchemaContractError(f"unknown dataset {dataset!r}") from exc


def date_column_for(dataset: str, *, contract_path: Path | str | None = None) -> str:
    """Return the row-date column used by the holdout date gate."""
    ds = _dataset_spec(dataset, contract_path=contract_path)
    col = ds.get("date_column")
    if not col:
        raise SchemaContractError(
            f"dataset {dataset!r} missing date_column in contract"
        )
    return str(col)


def arrow_schema_for(
    dataset: str,
    *,
    contract_path: Path | str | None = None,
) -> pa.Schema:
    """Arrow schema for required columns pinned by the declared contract."""
    ds = _dataset_spec(dataset, contract_path=contract_path)
    nullable = set(ds.get("nullable_columns") or [])
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
        fields.append(pa.field(name, arrow_type, nullable=name in nullable))
    return pa.schema(fields)


def types_compatible(expected: pa.DataType, actual: pa.DataType) -> bool:
    """Return whether ``actual`` satisfies the contracted ``expected`` type.

    string / large_string are interchangeable for sealed US large_string and
    KR/crypto string columns. Timestamp equality requires matching unit and
    timezone (including both-naive).
    """
    if expected.equals(actual):
        return True
    if (pa.types.is_string(expected) or pa.types.is_large_string(expected)) and (
        pa.types.is_string(actual) or pa.types.is_large_string(actual)
    ):
        return True
    if pa.types.is_timestamp(expected) and pa.types.is_timestamp(actual):
        return expected.unit == actual.unit and expected.tz == actual.tz
    return False


def enforce_table_load_policy(
    table: pa.Table,
    dataset: str,
    *,
    contract_path: Path | str | None = None,
) -> None:
    """Apply contract-declared post-schema gates to a loaded table.

    This is the **structural** attachment point for label/value policy: any
    caller that validates a table against a contract (including
    ``loader.load_shard`` and ``ContractBackedCorpusAdapter.load_shard``)
    must pass through ``validate_table_schema``, which invokes this function.
    Wrapper-only gates are insufficient and incomplete by construction.
    """
    ds = _dataset_spec(dataset, contract_path=contract_path)
    policy = ds.get("table_load_policy") or {}
    if not policy:
        return

    if policy.get("require_crypto_parquet_labels"):
        # Import locally to keep schema_contract usable without crypto package
        # for KR-only paths; only contracts that opt in pay this cost.
        from research.crypto_corpus.policy import policy_from_parquet_metadata

        policy_from_parquet_metadata(table.schema.metadata)

    meta_eq = policy.get("require_schema_metadata_equals") or {}
    if meta_eq:
        metadata = table.schema.metadata or {}
        for key, expected in meta_eq.items():
            key_b = key.encode("utf-8") if isinstance(key, str) else key
            exp_b = expected.encode("utf-8") if isinstance(expected, str) else expected
            actual = metadata.get(key_b)
            if actual != exp_b:
                raise ContractTablePolicyError(
                    f"dataset={dataset!r} required schema metadata "
                    f"{key!r}={expected!r} missing or mismatched "
                    f"(actual={actual!r}); refusing load"
                )

    required_values = policy.get("required_column_values") or {}
    for column, expected in required_values.items():
        if column not in table.column_names:
            raise ContractTablePolicyError(
                f"dataset={dataset!r} missing policy column {column!r}"
            )
        for i, raw in enumerate(table.column(column).to_pylist()):
            if raw is None or str(raw) != str(expected):
                raise ContractTablePolicyError(
                    f"dataset={dataset!r} row {i} column {column!r}="
                    f"{raw!r} is not required value {expected!r}; "
                    f"refusing load (no silent filter)"
                )


def validate_table_schema(
    table: pa.Table,
    dataset: str,
    *,
    contract_path: Path | str | None = None,
) -> None:
    """Refuse unless required columns match the contract (extras policy-aware).

    No silent column drop, rename, or dtype coercion. Extra columns are
    allowed only when the contract sets ``allow_extra_columns: true``.
    Columns listed under ``forbidden_columns`` always fail (used to keep US
    ``trading_value`` absent).

    After column/dtype checks, enforces any ``table_load_policy`` declared on
    the dataset (labels, required column values). That policy is structural:
    every load path that calls this function inherits it.
    """
    ds = _dataset_spec(dataset, contract_path=contract_path)
    required: list[str] = list(ds["required_columns"])
    nullable = set(ds.get("nullable_columns") or [])
    allow_extra = bool(ds.get("allow_extra_columns", False))
    forbidden = set(ds.get("forbidden_columns") or [])
    dtypes: dict[str, str] = ds["column_dtypes"]

    actual = table.schema
    act_names = list(actual.names)
    diffs: list[str] = []

    for name in forbidden:
        if name in act_names:
            diffs.append(
                f"forbidden column {name!r} present (contract declares it absent)"
            )

    for name in required:
        if name not in act_names:
            diffs.append(f"missing column {name!r}")
            continue
        expected_type = _ARROW_DTYPE_MAP[dtypes[name]]
        act_field = actual.field(name)
        if not types_compatible(expected_type, act_field.type):
            diffs.append(
                f"dtype {name!r}: expected={expected_type} actual={act_field.type}"
            )
        # Nullability: contracted non-null must not be nullable in the file.
        # Contracted nullable may be non-null or nullable.
        if name not in nullable and act_field.nullable:
            diffs.append(f"nullability {name!r}: expected non-null, actual nullable")

    if not allow_extra:
        unexpected = [n for n in act_names if n not in required]
        if unexpected:
            diffs.append(f"unexpected columns {unexpected}")

    if diffs:
        raise SchemaMismatchError(f"schema mismatch for dataset={dataset!r}: {diffs}")

    enforce_table_load_policy(table, dataset, contract_path=contract_path)
