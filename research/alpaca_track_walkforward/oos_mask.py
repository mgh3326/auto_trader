"""ROB-1062 H4 (AC22-AC25) — fail-closed OOS PnL masking.

``Masked`` never retains the source object, a closure over it, plaintext
payload bytes, ciphertext, or a caller-reachable reveal callable. ``mask``
encodes the value with a closed typed codec and transfers it into an isolated
local vault process. The public object contains binding metadata only.

Unmask authority is not a public dataclass constructor. H5 must call
``issue_all_folds_dry_count_pass`` with all 8 folds' exact ``BlindCounts``
objects and masked handles for one family/config. No vault token is issued
until every fold is complete and meets the sealed minimum. Evidence
reconstructed with ``object.__new__``, copied, subclassed, or mutated is
absent from the issuance registry and is rejected.

Native debuggers able to read another process's address space are outside the
threat model. Ordinary Python reflection, closure inspection, object graph
walking, private-name imports, copying, and serialization stay in the caller
process and cannot recover the vault payload.
"""

from __future__ import annotations

import atexit
import json
import math
import multiprocessing
import os
import threading
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import blind_counts as bc
import canonical_hash
import pnl_views as pv
import wf_seal_consumption as wf_seal

__all__ = [
    "DryCountPassEvidence",
    "Masked",
    "OOSMaskBypassError",
    "issue_all_folds_dry_count_pass",
    "issue_dry_count_pass",
    "mask",
    "unmask",
]


class OOSMaskBypassError(RuntimeError):
    """A masked value or PASS authority failed an integrity check."""


def _binding_tuple(
    *, fold_id: str, family: str, config_id: str
) -> tuple[str, str, str]:
    if (
        type(fold_id) is not str
        or type(family) is not str
        or type(config_id) is not str
    ):
        raise TypeError("mask binding values must be built-in str")
    return (fold_id, family, config_id)


def _counts_fingerprint(counts: bc.BlindCounts) -> str:
    payload = (
        counts.total_decision_records,
        counts.modeled_entries_count,
        counts.closed_trades_count,
        counts.open_positions_count,
        counts.entry_unfilled_count,
        counts.exit_unfilled_count,
        counts.fill_window_incomplete_count,
        counts.holding_days,
        tuple(sorted(counts.reason_code_histogram.items())),
        counts.is_incomplete,
    )
    return canonical_hash.canonical_sha256(payload)


def _finite_float(value: object, label: str) -> float:
    if type(value) is not float:
        raise OOSMaskBypassError(f"{label} must be an exact float")
    if not math.isfinite(value):
        raise OOSMaskBypassError(f"{label} must be finite")
    return value


def _encode_node(value: object) -> list:
    """Encode only the closed set of OOS result value types."""
    if value is None:
        return ["none", None]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", value]
    if type(value) is float:
        return ["float", _finite_float(value, "masked float").hex()]
    if type(value) is str:
        return ["str", value]
    if type(value) is list:
        return ["list", [_encode_node(item) for item in value]]
    if type(value) is tuple:
        return ["tuple", [_encode_node(item) for item in value]]
    if type(value) is dict:
        entries: list[list] = []
        for key, item in value.items():
            if type(key) is not str:
                raise OOSMaskBypassError("masked dict keys must be exact strings")
            entries.append([key, _encode_node(item)])
        return ["dict", sorted(entries, key=lambda pair: pair[0])]
    if type(value) is pv.ThreeViewPnL:
        scenarios: list[list[str]] = []
        for key, scenario_value in value.shadow_net_bp_by_scenario.items():
            if type(key) is not str:
                raise OOSMaskBypassError(
                    "ThreeViewPnL scenario keys must be exact strings"
                )
            scenarios.append(
                [
                    key,
                    _finite_float(
                        scenario_value, f"ThreeViewPnL scenario {key!r}"
                    ).hex(),
                ]
            )
        return [
            "three_view_pnl",
            {
                "gross_bp": _finite_float(
                    value.gross_bp, "ThreeViewPnL.gross_bp"
                ).hex(),
                "actual_fill_bp": _finite_float(
                    value.actual_fill_bp, "ThreeViewPnL.actual_fill_bp"
                ).hex(),
                "shadow_net_bp_by_scenario": sorted(
                    scenarios, key=lambda pair: pair[0]
                ),
            },
        ]
    raise OOSMaskBypassError(f"unsupported masked value type {type(value).__name__!r}")


def _decode_node(node: object) -> object:
    """Decode the closed wire schema and reject every malformed shape."""
    if type(node) is not list or len(node) != 2 or type(node[0]) is not str:
        raise OOSMaskBypassError("masked payload has an invalid typed node")
    tag, payload = node
    if tag == "none" and payload is None:
        return None
    if tag == "bool" and type(payload) is bool:
        return payload
    if tag == "int" and type(payload) is int:
        return payload
    if tag == "float" and type(payload) is str:
        try:
            return _finite_float(float.fromhex(payload), "decoded float")
        except ValueError as exc:
            raise OOSMaskBypassError("masked float payload is invalid") from exc
    if tag == "str" and type(payload) is str:
        return payload
    if tag in {"list", "tuple"} and type(payload) is list:
        values = [_decode_node(item) for item in payload]
        return values if tag == "list" else tuple(values)
    if tag == "dict" and type(payload) is list:
        result: dict[str, object] = {}
        for pair in payload:
            if (
                type(pair) is not list
                or len(pair) != 2
                or type(pair[0]) is not str
                or pair[0] in result
            ):
                raise OOSMaskBypassError("masked dict payload is invalid")
            result[pair[0]] = _decode_node(pair[1])
        return result
    if tag == "three_view_pnl" and type(payload) is dict:
        if set(payload) != {
            "gross_bp",
            "actual_fill_bp",
            "shadow_net_bp_by_scenario",
        }:
            raise OOSMaskBypassError("ThreeViewPnL payload fields are invalid")
        gross_bp = _decode_node(["float", payload["gross_bp"]])
        actual_fill_bp = _decode_node(["float", payload["actual_fill_bp"]])
        scenarios_payload = payload["shadow_net_bp_by_scenario"]
        if type(scenarios_payload) is not list:
            raise OOSMaskBypassError("ThreeViewPnL scenario payload is invalid")
        scenarios: dict[str, float] = {}
        for pair in scenarios_payload:
            if (
                type(pair) is not list
                or len(pair) != 2
                or type(pair[0]) is not str
                or pair[0] in scenarios
                or type(pair[1]) is not str
            ):
                raise OOSMaskBypassError("ThreeViewPnL scenario entry is invalid")
            scenario_value = _decode_node(["float", pair[1]])
            if type(scenario_value) is not float:
                raise OOSMaskBypassError("ThreeViewPnL scenario value is invalid")
            scenarios[pair[0]] = scenario_value
        if type(gross_bp) is not float or type(actual_fill_bp) is not float:
            raise OOSMaskBypassError("ThreeViewPnL scalar payload is invalid")
        return pv.ThreeViewPnL(
            gross_bp=gross_bp,
            actual_fill_bp=actual_fill_bp,
            shadow_net_bp_by_scenario=scenarios,
        )
    raise OOSMaskBypassError("masked payload uses an unknown or malformed type")


def _encode_masked_value(value: object) -> bytes:
    return json.dumps(
        _encode_node(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _decode_masked_value(payload: object) -> object:
    if type(payload) is not bytes:
        raise OOSMaskBypassError("vault returned a non-bytes masked payload")
    try:
        node = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OOSMaskBypassError("vault returned an invalid masked payload") from exc
    return _decode_node(node)


class Masked:
    """Opaque public handle. No payload, vault id, key, or reveal slot."""

    __slots__ = ("_fold_id", "_family", "_config_id", "__weakref__")

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise OOSMaskBypassError("Masked instances can only be issued by mask()")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("Masked cannot be subclassed")

    @property
    def fold_id(self) -> str:
        return object.__getattribute__(self, "_fold_id")

    @property
    def family(self) -> str:
        return object.__getattribute__(self, "_family")

    @property
    def config_id(self) -> str:
        return object.__getattribute__(self, "_config_id")

    def __setattr__(self, name: str, value: Any) -> None:
        raise OOSMaskBypassError("Masked values are immutable")

    def __delattr__(self, name: str) -> None:
        raise OOSMaskBypassError("Masked values are immutable")

    def __repr__(self) -> str:
        return (
            f"Masked(fold_id={self.fold_id!r}, family={self.family!r}, "
            f"config_id={self.config_id!r}, value=<masked>)"
        )

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise OOSMaskBypassError("Masked values cannot be copied or pickled")

    def __reduce_ex__(self, protocol: int) -> Any:
        raise OOSMaskBypassError("Masked values cannot be copied or pickled")

    def __getstate__(self) -> Any:
        raise OOSMaskBypassError("Masked values cannot be serialized")

    def __copy__(self) -> Any:
        raise OOSMaskBypassError("Masked values cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Any:
        raise OOSMaskBypassError("Masked values cannot be deep-copied")

    def __eq__(self, other: object) -> bool:
        raise OOSMaskBypassError("Masked values cannot be compared")

    __hash__ = None  # type: ignore[assignment]


class DryCountPassEvidence:
    """Registry-backed evidence issued only after all 8 folds pass."""

    __slots__ = (
        "_family",
        "_config_id",
        "_fold_ids",
        "_modeled_entries_by_fold",
        "_min_modeled_entries_per_fold",
        "__weakref__",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise OOSMaskBypassError(
            "DryCountPassEvidence cannot be constructed externally; "
            "call issue_all_folds_dry_count_pass(...)"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("DryCountPassEvidence cannot be subclassed")

    @property
    def fold_ids(self) -> tuple[str, ...]:
        return object.__getattribute__(self, "_fold_ids")

    @property
    def family(self) -> str:
        return object.__getattribute__(self, "_family")

    @property
    def config_id(self) -> str:
        return object.__getattribute__(self, "_config_id")

    @property
    def modeled_entries_by_fold(self) -> tuple[tuple[str, int], ...]:
        return object.__getattribute__(self, "_modeled_entries_by_fold")

    @property
    def min_modeled_entries_per_fold(self) -> int:
        return object.__getattribute__(self, "_min_modeled_entries_per_fold")

    @property
    def passed(self) -> bool:
        return True

    def binding_key(self) -> tuple[str, str, tuple[str, ...]]:
        return (self.family, self.config_id, self.fold_ids)

    def __setattr__(self, name: str, value: Any) -> None:
        raise OOSMaskBypassError("DryCountPassEvidence is immutable")

    def __delattr__(self, name: str) -> None:
        raise OOSMaskBypassError("DryCountPassEvidence is immutable")

    def __repr__(self) -> str:
        return (
            "DryCountPassEvidence("
            f"fold_ids={self.fold_ids!r}, family={self.family!r}, "
            f"config_id={self.config_id!r}, "
            f"modeled_entries_by_fold={self.modeled_entries_by_fold!r}, "
            f"min_modeled_entries_per_fold={self.min_modeled_entries_per_fold}, "
            "passed=True)"
        )

    def __reduce__(self) -> Any:
        raise OOSMaskBypassError("PASS evidence cannot be copied or serialized")

    def __reduce_ex__(self, protocol: int) -> Any:
        raise OOSMaskBypassError("PASS evidence cannot be copied or serialized")

    def __copy__(self) -> Any:
        raise OOSMaskBypassError("PASS evidence cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Any:
        raise OOSMaskBypassError("PASS evidence cannot be deep-copied")


def _vault_worker(connection: Any) -> None:
    """Own plaintext payloads and PASS tokens outside the caller process."""
    masks: dict[int, dict[str, Any]] = {}
    passes: dict[bytes, int] = {}
    next_handle = 1
    while True:
        try:
            request = connection.recv()
        except EOFError:
            return
        operation = request[0]
        try:
            if operation == "CREATE":
                (
                    _operation,
                    payload,
                    binding,
                    counts_fingerprint,
                    modeled_entries,
                    is_incomplete,
                    sealed_minimum,
                ) = request
                handle = next_handle
                next_handle += 1
                masks[handle] = {
                    "payload": payload,
                    "binding": binding,
                    "counts_fingerprint": counts_fingerprint,
                    "modeled_entries": modeled_entries,
                    "is_incomplete": is_incomplete,
                    "sealed_minimum": sealed_minimum,
                    "consumed": False,
                }
                connection.send(("OK", handle))
            elif operation == "ISSUE":
                _operation, handle, counts_fingerprint = request
                record = masks.get(handle)
                if record is None:
                    raise OOSMaskBypassError("unknown masked handle")
                if counts_fingerprint != record["counts_fingerprint"]:
                    raise OOSMaskBypassError("dry-count fingerprint mismatch")
                if record["is_incomplete"]:
                    raise OOSMaskBypassError(
                        "incomplete dry counts can never issue PASS"
                    )
                if record["modeled_entries"] < record["sealed_minimum"]:
                    raise OOSMaskBypassError(
                        "bound dry count is below the sealed minimum modeled entries"
                    )
                token = os.urandom(32)
                passes[token] = handle
                connection.send(("OK", token))
            elif operation == "REVEAL":
                _operation, handle, token = request
                record = masks.get(handle)
                if record is not None and record["consumed"]:
                    raise OOSMaskBypassError(
                        "masked OOS PnL has already been unmasked once"
                    )
                if record is None or passes.get(token) != handle:
                    raise OOSMaskBypassError("PASS token is not valid for this mask")
                record["consumed"] = True
                passes.pop(token, None)
                payload = record["payload"]
                record["payload"] = None
                connection.send(("OK", payload))
            elif operation == "DROP":
                _operation, handle = request
                masks.pop(handle, None)
                for token, token_handle in tuple(passes.items()):
                    if token_handle == handle:
                        passes.pop(token, None)
                connection.send(("OK", None))
            elif operation == "STOP":
                connection.send(("OK", None))
                return
            else:
                raise OOSMaskBypassError("unknown vault operation")
        except Exception as exc:
            connection.send(("ERROR", type(exc).__name__, str(exc)))


class _VaultClient:
    __slots__ = ("_connection", "_lock", "_process")

    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_vault_worker,
            args=(child,),
            name="rob1062-oos-mask-vault",
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._lock = threading.RLock()
        self._process = process

    def call(self, request: tuple[Any, ...]) -> Any:
        with self._lock:
            self._connection.send(request)
            response = self._connection.recv()
        if response[0] == "OK":
            return response[1]
        raise OOSMaskBypassError(response[2])

    def stop(self) -> None:
        if self._process.is_alive():
            try:
                self.call(("STOP",))
            except (EOFError, OSError, OOSMaskBypassError):
                pass
            self._process.join(timeout=1.0)
        self._connection.close()


@dataclass(slots=True)
class _MaskRecord:
    masked_ref: weakref.ReferenceType[Masked]
    binding: tuple[str, str, str]
    vault_handle: int
    counts: bc.BlindCounts
    counts_fingerprint: str


@dataclass(slots=True)
class _EvidenceRecord:
    evidence_ref: weakref.ReferenceType[DryCountPassEvidence]
    family: str
    config_id: str
    fold_ids: tuple[str, ...]
    binding_by_masked_id: Mapping[int, tuple[str, str, str]]
    counts_fingerprint_by_masked_id: Mapping[int, str]
    vault_token_by_masked_id: Mapping[int, bytes]


_VAULT: _VaultClient | None = None
_VAULT_LOCK = threading.RLock()
_MASKS: dict[int, _MaskRecord] = {}
_EVIDENCES: dict[int, _EvidenceRecord] = {}


def _vault() -> _VaultClient:
    global _VAULT
    with _VAULT_LOCK:
        if _VAULT is None:
            _VAULT = _VaultClient()
        return _VAULT


def _stop_vault() -> None:
    global _VAULT
    with _VAULT_LOCK:
        if _VAULT is not None:
            _VAULT.stop()
            _VAULT = None


atexit.register(_stop_vault)


def _drop_mask(masked_id: int, vault_handle: int):
    def discard(_ref: weakref.ReferenceType[Masked]) -> None:
        _MASKS.pop(masked_id, None)
        if _VAULT is None:
            return
        try:
            _VAULT.call(("DROP", vault_handle))
        except (EOFError, OSError, OOSMaskBypassError):
            pass

    return discard


def _drop_evidence(evidence_id: int):
    def discard(_ref: weakref.ReferenceType[DryCountPassEvidence]) -> None:
        _EVIDENCES.pop(evidence_id, None)

    return discard


def _record_for(masked: Masked) -> _MaskRecord:
    if type(masked) is not Masked:
        raise TypeError("expected an exact Masked instance")
    record = _MASKS.get(id(masked))
    if record is None or record.masked_ref() is not masked:
        raise OOSMaskBypassError("masked handle is reconstructed or not issued")
    if (masked.fold_id, masked.family, masked.config_id) != record.binding:
        raise OOSMaskBypassError("masked binding integrity check failed")
    return record


def mask(
    raw_value: Any,
    *,
    fold_id: str,
    family: str,
    config_id: str,
    dry_counts: bc.BlindCounts,
) -> Masked:
    """Transfer one OOS value to the vault and bind its actual dry counts."""
    if type(dry_counts) is not bc.BlindCounts:
        raise TypeError("dry_counts must be an exact BlindCounts instance")
    binding = _binding_tuple(fold_id=fold_id, family=family, config_id=config_id)
    counts_fingerprint = _counts_fingerprint(dry_counts)
    payload = _encode_masked_value(raw_value)
    try:
        vault_handle = _vault().call(
            (
                "CREATE",
                payload,
                binding,
                counts_fingerprint,
                dry_counts.modeled_entries_count,
                dry_counts.is_incomplete,
                wf_seal.min_modeled_entries_per_fold(),
            )
        )
    finally:
        del payload

    masked = object.__new__(Masked)
    object.__setattr__(masked, "_fold_id", fold_id)
    object.__setattr__(masked, "_family", family)
    object.__setattr__(masked, "_config_id", config_id)
    masked_id = id(masked)
    masked_ref = weakref.ref(masked, _drop_mask(masked_id, vault_handle))
    _MASKS[masked_id] = _MaskRecord(
        masked_ref=masked_ref,
        binding=binding,
        vault_handle=vault_handle,
        counts=dry_counts,
        counts_fingerprint=counts_fingerprint,
    )
    return masked


def issue_dry_count_pass(
    masked: Masked, dry_counts: bc.BlindCounts
) -> DryCountPassEvidence:
    """The former fold-local authority is deliberately disabled."""
    _record_for(masked)
    if type(dry_counts) is not bc.BlindCounts:
        raise TypeError("dry_counts must be an exact BlindCounts instance")
    raise OOSMaskBypassError(
        "fold-local PASS is forbidden; all 8 folds must pass together"
    )


def issue_all_folds_dry_count_pass(
    *,
    masked_by_fold: Mapping[str, Sequence[Masked]],
    dry_counts_by_fold: Mapping[str, bc.BlindCounts],
) -> DryCountPassEvidence:
    """Issue one authority only after the exact 8-fold dry gate passes."""
    expected_fold_ids = tuple(f"fold-{index}" for index in range(wf_seal.oos_folds()))
    if tuple(sorted(masked_by_fold)) != expected_fold_ids:
        raise OOSMaskBypassError("masked_by_fold must cover exactly fold-0..fold-7")
    if tuple(sorted(dry_counts_by_fold)) != expected_fold_ids:
        raise OOSMaskBypassError("dry_counts_by_fold must cover exactly fold-0..fold-7")

    sealed_minimum = wf_seal.min_modeled_entries_per_fold()
    family: str | None = None
    config_id: str | None = None
    records_by_masked_id: dict[int, _MaskRecord] = {}
    fingerprints_by_masked_id: dict[int, str] = {}

    # Validate every fold before asking the vault to issue any token.
    for fold_id in expected_fold_ids:
        counts = dry_counts_by_fold[fold_id]
        if type(counts) is not bc.BlindCounts:
            raise TypeError("every dry count must be an exact BlindCounts instance")
        if counts.is_incomplete:
            raise OOSMaskBypassError(
                f"{fold_id} is incomplete; aggregate PASS cannot issue"
            )
        if counts.modeled_entries_count < sealed_minimum:
            raise OOSMaskBypassError(
                f"{fold_id} is below the sealed minimum modeled entries"
            )
        if not masked_by_fold[fold_id]:
            raise OOSMaskBypassError(
                f"{fold_id} has no H4-issued dry-count gate handle"
            )
        for masked in masked_by_fold[fold_id]:
            record = _record_for(masked)
            if record.binding[0] != fold_id:
                raise OOSMaskBypassError("masked handle is filed under the wrong fold")
            if record.counts is not counts:
                raise OOSMaskBypassError(
                    "dry counts are not the actual object bound by H4 to this mask"
                )
            fingerprint = _counts_fingerprint(counts)
            if fingerprint != record.counts_fingerprint:
                raise OOSMaskBypassError("bound dry counts changed after mask issuance")
            if family is None:
                family, config_id = record.binding[1], record.binding[2]
            elif (record.binding[1], record.binding[2]) != (family, config_id):
                raise OOSMaskBypassError(
                    "aggregate PASS cannot mix family/config identities"
                )
            records_by_masked_id[id(masked)] = record
            fingerprints_by_masked_id[id(masked)] = fingerprint

    if family is None or config_id is None:
        raise OOSMaskBypassError(
            "aggregate PASS requires at least one actual masked H4 value"
        )

    tokens_by_masked_id = {
        masked_id: _vault().call(
            (
                "ISSUE",
                record.vault_handle,
                fingerprints_by_masked_id[masked_id],
            )
        )
        for masked_id, record in records_by_masked_id.items()
    }
    evidence = object.__new__(DryCountPassEvidence)
    for name, value in (
        ("_family", family),
        ("_config_id", config_id),
        ("_fold_ids", expected_fold_ids),
        (
            "_modeled_entries_by_fold",
            tuple(
                (fold_id, dry_counts_by_fold[fold_id].modeled_entries_count)
                for fold_id in expected_fold_ids
            ),
        ),
        ("_min_modeled_entries_per_fold", sealed_minimum),
    ):
        object.__setattr__(evidence, name, value)
    evidence_id = id(evidence)
    evidence_ref = weakref.ref(evidence, _drop_evidence(evidence_id))
    _EVIDENCES[evidence_id] = _EvidenceRecord(
        evidence_ref=evidence_ref,
        family=family,
        config_id=config_id,
        fold_ids=expected_fold_ids,
        binding_by_masked_id={
            masked_id: record.binding
            for masked_id, record in records_by_masked_id.items()
        },
        counts_fingerprint_by_masked_id=dict(fingerprints_by_masked_id),
        vault_token_by_masked_id=tokens_by_masked_id,
    )
    return evidence


def unmask(masked: Masked, evidence: DryCountPassEvidence) -> Any:
    """One-shot reveal with registry-issued, matching PASS evidence."""
    record = _record_for(masked)
    if type(evidence) is not DryCountPassEvidence:
        raise TypeError("expected an exact DryCountPassEvidence instance")
    evidence_record = _EVIDENCES.get(id(evidence))
    masked_id = id(masked)
    if (
        evidence_record is None
        or evidence_record.evidence_ref() is not evidence
        or evidence_record.binding_by_masked_id.get(masked_id) != record.binding
        or evidence.binding_key()
        != (
            evidence_record.family,
            evidence_record.config_id,
            evidence_record.fold_ids,
        )
        or evidence_record.counts_fingerprint_by_masked_id.get(masked_id)
        != record.counts_fingerprint
    ):
        raise OOSMaskBypassError(
            "PASS evidence was reconstructed, mutated, or issued for another mask"
        )
    payload = _vault().call(
        (
            "REVEAL",
            record.vault_handle,
            evidence_record.vault_token_by_masked_id[masked_id],
        )
    )
    try:
        return _decode_masked_value(payload)
    finally:
        del payload
