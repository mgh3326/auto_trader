"""Declared schema contract for the KR backtest harness and market adapters.

SCHEMA_ORIGIN = SEALED_CORPUS_V1

Contracts declare the **sealed corpus** column names and types after contrast
against real parquet. Adapters own an explicit mapping layer from those
columns onto harness bar objects. Silent rename/coercion outside the declared
``harness_column_map`` is forbidden.

**Table-load policy is NOT file self-declaration.** For a recognized corpus
identity (``corpus_id``), the required policy is looked up from the code
registry ``CORPUS_TABLE_LOAD_POLICY_BY_ID``. A contract JSON that omits or
empties ``table_load_policy`` cannot weaken those gates. Policy absence for a
known corpus/dataset is a hard error — never ``or {}`` success.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa

__all__ = [
    "CONTRACT_PATH",
    "CORPUS_TABLE_LOAD_POLICY_BY_ID",
    "SCHEMA_ORIGIN",
    "SchemaContractError",
    "SchemaMismatchError",
    "ContractTablePolicyError",
    "arrow_schema_for",
    "date_column_for",
    "enforce_table_load_policy",
    "load_contract",
    "required_table_load_policy",
    "types_compatible",
    "validate_table_schema",
]

SCHEMA_ORIGIN = "SEALED_CORPUS_V1"
CONTRACT_PATH = Path(__file__).resolve().parent / "schema_contract.v1.json"

# Authoritative post-schema gates keyed by corpus_id (not by whatever the
# JSON file chooses to declare). Removing policy keys from a contract copy
# must not admit unlabeled or wrong-frequency data.
#
# Each dataset entry is a non-empty mapping. Use
# ``{"schema_column_gates_only": True}`` when the corpus needs column/dtype
# refusal only (no parquet-metadata labels).
CORPUS_TABLE_LOAD_POLICY_BY_ID: dict[str, dict[str, dict[str, Any]]] = {
    "kr-corpus-v1": {
        "ohlcv": {"schema_column_gates_only": True},
        "membership": {"schema_column_gates_only": True},
    },
    "crypto-corpus-v1": {
        "ohlcv": {
            "require_crypto_parquet_labels": True,
            "required_column_values": {"frequency": "1d"},
        },
        # Harness membership for crypto adapters is fixture-only if present;
        # still refuse silent empty policy.
        "membership": {"schema_column_gates_only": True},
    },
    "us-corpus-v1": {
        "ohlcv": {
            "require_schema_metadata_equals": {"SURVIVORSHIP_BIASED": "TRUE"},
        },
        "membership": {"schema_column_gates_only": True},
    },
}

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
    """Corpus-identity table policy refused the load.

    Raised for required column *values* (e.g. frequency must be ``1d``),
    missing/invalid policy identity, and metadata label refusals that are
    expressed as contract policy errors. Venue-specific unlabeled errors
    (e.g. ``UnlabeledParquetError``) may also propagate from nested checks.
    """


def load_contract(contract_path: Path | str | None = None) -> dict[str, Any]:
    """Load a declared local JSON contract (schema columns + corpus identity).

    Requires ``schema_origin == SEALED_CORPUS_V1`` and a known ``corpus_id``.
    Table-load policy is **not** taken from this file as authority; see
    ``required_table_load_policy``.

    Caveat (operator-confirmed residual): callers may pass an arbitrary
    ``contract_path`` (same class as raw-PyArrow residual). Supported-path
    fail-closed means: if the file claims a known sealed corpus identity,
    the code registry policy applies and cannot be stripped away. Completely
    custom identities outside the registry fail closed at load.
    """
    path = Path(contract_path) if contract_path is not None else CONTRACT_PATH
    raw = path.read_text(encoding="utf-8")
    contract = json.loads(raw)
    if contract.get("schema_origin") != SCHEMA_ORIGIN:
        raise SchemaContractError(
            f"schema_origin must be {SCHEMA_ORIGIN!r}, "
            f"got {contract.get('schema_origin')!r}"
        )
    corpus_id = contract.get("corpus_id")
    if not isinstance(corpus_id, str) or not corpus_id:
        raise SchemaContractError(
            "corpus_id must be a non-empty string identifying the sealed corpus"
        )
    if corpus_id not in CORPUS_TABLE_LOAD_POLICY_BY_ID:
        raise SchemaContractError(
            f"unknown corpus_id {corpus_id!r}; not in "
            f"{sorted(CORPUS_TABLE_LOAD_POLICY_BY_ID)!r}"
        )
    return contract


def required_table_load_policy(
    corpus_id: str,
    dataset: str,
) -> dict[str, Any]:
    """Return the code-registry policy for ``corpus_id`` × ``dataset``.

    Fail-closed: missing corpus, missing dataset, or empty policy mapping
    all raise. Never synthesizes ``{}``.
    """
    if corpus_id not in CORPUS_TABLE_LOAD_POLICY_BY_ID:
        raise ContractTablePolicyError(
            f"no table_load_policy registry entry for corpus_id={corpus_id!r}"
        )
    by_dataset = CORPUS_TABLE_LOAD_POLICY_BY_ID[corpus_id]
    if dataset not in by_dataset:
        raise ContractTablePolicyError(
            f"corpus_id={corpus_id!r} has no table_load_policy for "
            f"dataset={dataset!r}; refusing load"
        )
    policy = by_dataset[dataset]
    if not isinstance(policy, dict) or not policy:
        raise ContractTablePolicyError(
            f"corpus_id={corpus_id!r} dataset={dataset!r} registry policy "
            f"must be a non-empty dict; got {policy!r}"
        )
    return policy


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
    """Apply corpus-identity post-schema gates (registry, not file self-declare).

    Identity path: load contract → ``corpus_id`` →
    ``CORPUS_TABLE_LOAD_POLICY_BY_ID[corpus_id][dataset]``. A stripped
    ``table_load_policy`` key in a JSON copy cannot disable gates.
    """
    contract = load_contract(contract_path)
    corpus_id = str(contract["corpus_id"])
    policy = required_table_load_policy(corpus_id, dataset)

    # Optional: if the file also declares a policy block, it must not *weaken*
    # the registry (presence of empty/null file policy is ignored; only
    # registry is authoritative). Wrong-type file policy is refused so a
    # caller cannot claim "I declared policy=null means no check".
    file_ds = contract.get("datasets", {}).get(dataset) or {}
    if "table_load_policy" in file_ds:
        file_policy = file_ds["table_load_policy"]
        if file_policy is None or not isinstance(file_policy, dict):
            raise ContractTablePolicyError(
                f"corpus_id={corpus_id!r} dataset={dataset!r} file "
                f"table_load_policy must be a dict when present; "
                f"got {type(file_policy).__name__}"
            )
        if not file_policy:
            raise ContractTablePolicyError(
                f"corpus_id={corpus_id!r} dataset={dataset!r} file "
                f"table_load_policy is empty; refusing fail-open self-declaration"
            )

    if policy.get("schema_column_gates_only"):
        # Column/dtype already checked by validate_table_schema.
        return

    if policy.get("require_crypto_parquet_labels"):
        from research.crypto_corpus.policy import policy_from_parquet_metadata

        policy_from_parquet_metadata(table.schema.metadata)

    meta_eq = policy.get("require_schema_metadata_equals")
    if meta_eq is not None:
        if not isinstance(meta_eq, dict) or not meta_eq:
            raise ContractTablePolicyError(
                f"require_schema_metadata_equals must be a non-empty dict "
                f"for corpus_id={corpus_id!r}"
            )
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

    required_values = policy.get("required_column_values")
    if required_values is not None:
        if not isinstance(required_values, dict) or not required_values:
            raise ContractTablePolicyError(
                f"required_column_values must be a non-empty dict "
                f"for corpus_id={corpus_id!r}"
            )
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

    After column/dtype checks, enforces corpus-identity ``table_load_policy``
    from the code registry (labels, required column values). That policy is
    structural: every load path that calls this function inherits it, and a
    contract file cannot opt out by omitting the policy key.
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
