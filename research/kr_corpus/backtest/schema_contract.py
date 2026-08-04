"""Sealed-corpus schema contracts selected by ``CorpusKind`` only.

SCHEMA_ORIGIN = SEALED_CORPUS_V1

Callers **do not** choose a contract file path. They pass a ``CorpusKind``
enum; the committed contract path and table-load policy are resolved
internally. That removes the R5 class of attack (swap ``corpus_id`` in a
caller-supplied JSON copy to pick a weaker registry policy).

Table-load policy authority is the **internal** registry
``_CORPUS_TABLE_LOAD_POLICY_BY_ID`` (not in ``__all__``), keyed by the corpus
identity of the selected ``CorpusKind``.

Caveat (honest residual): MappingProxyType blocks *naive in-place mutation*
of the registry object. It does **not** stop Python module-attribute monkeypatch
(``schema_contract._CORPUS_TABLE_LOAD_POLICY_BY_ID = evil`` or similar). That
class of bypass is the same as raw-PyArrow: outside the supported fail-closed
surface and not claimed blocked.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyarrow as pa

__all__ = [
    "CONTRACT_PATH",
    "CorpusKind",
    "SCHEMA_ORIGIN",
    "SchemaContractError",
    "SchemaMismatchError",
    "ContractTablePolicyError",
    "arrow_schema_for",
    "contract_path_for",
    "date_column_for",
    "enforce_table_load_policy",
    "load_contract",
    "required_table_load_policy",
    "types_compatible",
    "validate_table_schema",
]

SCHEMA_ORIGIN = "SEALED_CORPUS_V1"

_PKG = Path(__file__).resolve().parent
# KR default contract path constant (not a public selector).
CONTRACT_PATH = _PKG / "schema_contract.v1.json"
_CRYPTO_CONTRACT_PATH = (
    _PKG / "market_adapters" / "contracts" / "crypto-corpus-v1.schema.json"
)
_US_CONTRACT_PATH = _PKG / "market_adapters" / "contracts" / "us-corpus-v1.schema.json"


class CorpusKind(Enum):
    """Supported sealed corpora. The only public contract selector."""

    KR_V1 = "kr-corpus-v1"
    CRYPTO_V1 = "crypto-corpus-v1"
    US_V1 = "us-corpus-v1"


# Kind → committed contract file (internal only; not caller-writable).
_CORPUS_CONTRACT_PATHS: dict[CorpusKind, Path] = {
    CorpusKind.KR_V1: CONTRACT_PATH,
    CorpusKind.CRYPTO_V1: _CRYPTO_CONTRACT_PATH,
    CorpusKind.US_V1: _US_CONTRACT_PATH,
}


def _freeze_mapping(value: Any) -> Any:
    """Recursively wrap dicts in MappingProxyType (naive mutation → TypeError)."""
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze_mapping(v) for k, v in value.items()})
    return value


# Internal post-schema gates keyed by corpus_id. Not a public API export.
# MappingProxyType stops naive ``registry[k] = …`` / nested item assignment;
# it does not stop ``schema_contract._CORPUS_TABLE_LOAD_POLICY_BY_ID = evil``.
_CORPUS_TABLE_LOAD_POLICY_BY_ID: Mapping[str, Any] = _freeze_mapping(
    {
        "kr-corpus-v1": {
            "ohlcv": {"schema_column_gates_only": True},
            "membership": {"schema_column_gates_only": True},
        },
        "crypto-corpus-v1": {
            "ohlcv": {
                "require_crypto_parquet_labels": True,
                "required_column_values": {"frequency": "1d"},
            },
            "membership": {"schema_column_gates_only": True},
        },
        "us-corpus-v1": {
            "ohlcv": {
                "require_schema_metadata_equals": {"SURVIVORSHIP_BIASED": "TRUE"},
            },
            "membership": {"schema_column_gates_only": True},
        },
    }
)

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
    """Corpus-identity table policy refused the load."""


def contract_path_for(corpus: CorpusKind) -> Path:
    """Return the committed contract path for ``corpus`` (read-only helper)."""
    if not isinstance(corpus, CorpusKind):
        raise SchemaContractError(
            f"corpus must be CorpusKind, got {type(corpus).__name__}"
        )
    return _CORPUS_CONTRACT_PATHS[corpus]


def load_contract(corpus: CorpusKind = CorpusKind.KR_V1) -> dict[str, Any]:
    """Load the committed contract for ``corpus`` and check identity binding.

    The file path is chosen internally from ``CorpusKind``. The on-disk JSON
    must declare ``corpus_id`` equal to ``corpus.value`` and
    ``schema_origin == SEALED_CORPUS_V1``. Callers cannot point at a temp copy.
    """
    if not isinstance(corpus, CorpusKind):
        raise SchemaContractError(
            f"corpus must be CorpusKind, got {type(corpus).__name__}"
        )
    path = contract_path_for(corpus)
    raw = path.read_text(encoding="utf-8")
    contract = json.loads(raw)
    if contract.get("schema_origin") != SCHEMA_ORIGIN:
        raise SchemaContractError(
            f"schema_origin must be {SCHEMA_ORIGIN!r}, "
            f"got {contract.get('schema_origin')!r}"
        )
    corpus_id = contract.get("corpus_id")
    if corpus_id != corpus.value:
        raise SchemaContractError(
            f"committed contract at {path} has corpus_id={corpus_id!r} "
            f"but CorpusKind requires {corpus.value!r}"
        )
    if corpus_id not in _CORPUS_TABLE_LOAD_POLICY_BY_ID:
        raise SchemaContractError(
            f"unknown corpus_id {corpus_id!r}; not in "
            f"{sorted(_CORPUS_TABLE_LOAD_POLICY_BY_ID)!r}"
        )
    return contract


def required_table_load_policy(
    corpus_id: str,
    dataset: str,
) -> Mapping[str, Any]:
    """Return the code-registry policy for ``corpus_id`` × ``dataset``."""
    if corpus_id not in _CORPUS_TABLE_LOAD_POLICY_BY_ID:
        raise ContractTablePolicyError(
            f"no table_load_policy registry entry for corpus_id={corpus_id!r}"
        )
    by_dataset = _CORPUS_TABLE_LOAD_POLICY_BY_ID[corpus_id]
    if dataset not in by_dataset:
        raise ContractTablePolicyError(
            f"corpus_id={corpus_id!r} has no table_load_policy for "
            f"dataset={dataset!r}; refusing load"
        )
    policy = by_dataset[dataset]
    if not isinstance(policy, Mapping) or not policy:
        raise ContractTablePolicyError(
            f"corpus_id={corpus_id!r} dataset={dataset!r} registry policy "
            f"must be a non-empty mapping; got {policy!r}"
        )
    return policy


def _dataset_spec(dataset: str, *, corpus: CorpusKind) -> dict[str, Any]:
    contract = load_contract(corpus)
    try:
        return contract["datasets"][dataset]
    except KeyError as exc:
        raise SchemaContractError(f"unknown dataset {dataset!r}") from exc


def date_column_for(dataset: str, *, corpus: CorpusKind = CorpusKind.KR_V1) -> str:
    """Return the row-date column used by the holdout date gate."""
    ds = _dataset_spec(dataset, corpus=corpus)
    col = ds.get("date_column")
    if not col:
        raise SchemaContractError(
            f"dataset {dataset!r} missing date_column in contract"
        )
    return str(col)


def arrow_schema_for(
    dataset: str,
    *,
    corpus: CorpusKind = CorpusKind.KR_V1,
) -> pa.Schema:
    """Arrow schema for required columns pinned by the selected corpus."""
    ds = _dataset_spec(dataset, corpus=corpus)
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
    """Return whether ``actual`` satisfies the contracted ``expected`` type."""
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
    corpus: CorpusKind,
) -> None:
    """Apply registry policy for the selected ``CorpusKind``."""
    if not isinstance(corpus, CorpusKind):
        raise SchemaContractError(
            f"corpus must be CorpusKind, got {type(corpus).__name__}"
        )
    # Bind identity from kind (not from a caller-supplied file claim).
    corpus_id = corpus.value
    policy = required_table_load_policy(corpus_id, dataset)
    # Still load committed file to ensure it matches kind (tamper on disk).
    load_contract(corpus)

    if policy.get("schema_column_gates_only"):
        return

    if policy.get("require_crypto_parquet_labels"):
        from research.crypto_corpus.policy import policy_from_parquet_metadata

        policy_from_parquet_metadata(table.schema.metadata)

    meta_eq = policy.get("require_schema_metadata_equals")
    if meta_eq is not None:
        if not isinstance(meta_eq, Mapping) or not meta_eq:
            raise ContractTablePolicyError(
                f"require_schema_metadata_equals must be a non-empty mapping "
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
        if not isinstance(required_values, Mapping) or not required_values:
            raise ContractTablePolicyError(
                f"required_column_values must be a non-empty mapping "
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
    corpus: CorpusKind = CorpusKind.KR_V1,
) -> None:
    """Refuse unless table matches the corpus selected by ``CorpusKind``.

    No ``contract_path`` argument exists. Schema columns come from the
    committed file bound to ``corpus``; load policy comes from the registry
    for ``corpus.value``.
    """
    if not isinstance(corpus, CorpusKind):
        raise SchemaContractError(
            f"corpus must be CorpusKind, got {type(corpus).__name__}"
        )
    ds = _dataset_spec(dataset, corpus=corpus)
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
        if name not in nullable and act_field.nullable:
            diffs.append(f"nullability {name!r}: expected non-null, actual nullable")

    if not allow_extra:
        unexpected = [n for n in act_names if n not in required]
        if unexpected:
            diffs.append(f"unexpected columns {unexpected}")

    if diffs:
        raise SchemaMismatchError(f"schema mismatch for dataset={dataset!r}: {diffs}")

    enforce_table_load_policy(table, dataset, corpus=corpus)
