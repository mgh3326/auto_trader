"""ROB-1062 H4 (AC22-AC25) — fail-closed OOS PnL masking.

``Masked`` never retains the source object, a closure over it, plaintext
pickle bytes, ciphertext, or a caller-reachable reveal callable. ``mask``
serializes the value immediately and transfers it into an isolated local
vault process. The public object contains binding metadata only.

Unmask authority is not a public dataclass constructor. H5 must call
``issue_dry_count_pass`` with the exact ``BlindCounts`` object that H4 bound
to the masked value at creation. Issuance rechecks that object's fingerprint,
the sealed minimum-entry threshold, and incomplete status inside both the
caller and the isolated vault. Evidence reconstructed with ``object.__new__``,
copied, subclassed, or mutated is absent from the issuance registry and is
rejected.

Native debuggers able to read another process's address space are outside the
threat model. Ordinary Python reflection, closure inspection, object graph
walking, private-name imports, copying, and serialization stay in the caller
process and cannot recover the vault payload.
"""

from __future__ import annotations

import atexit
import hashlib
import multiprocessing
import os
import pickle
import threading
import weakref
from dataclasses import dataclass
from typing import Any

import blind_counts as bc
import wf_seal_consumption as wf_seal

__all__ = [
    "DryCountPassEvidence",
    "Masked",
    "OOSMaskBypassError",
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
    return hashlib.sha256(pickle.dumps(payload, protocol=5)).hexdigest()


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
    """Registry-backed evidence issued only for an actual bound dry count."""

    __slots__ = (
        "_fold_id",
        "_family",
        "_config_id",
        "_modeled_entries",
        "_min_modeled_entries_per_fold",
        "__weakref__",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise OOSMaskBypassError(
            "DryCountPassEvidence cannot be constructed externally; "
            "call issue_dry_count_pass(masked, actual_blind_counts)"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("DryCountPassEvidence cannot be subclassed")

    @property
    def fold_id(self) -> str:
        return object.__getattribute__(self, "_fold_id")

    @property
    def family(self) -> str:
        return object.__getattribute__(self, "_family")

    @property
    def config_id(self) -> str:
        return object.__getattribute__(self, "_config_id")

    @property
    def modeled_entries(self) -> int:
        return object.__getattribute__(self, "_modeled_entries")

    @property
    def min_modeled_entries_per_fold(self) -> int:
        return object.__getattribute__(self, "_min_modeled_entries_per_fold")

    @property
    def passed(self) -> bool:
        return True

    def binding_key(self) -> tuple[str, str, str]:
        return (self.fold_id, self.family, self.config_id)

    def __setattr__(self, name: str, value: Any) -> None:
        raise OOSMaskBypassError("DryCountPassEvidence is immutable")

    def __delattr__(self, name: str) -> None:
        raise OOSMaskBypassError("DryCountPassEvidence is immutable")

    def __repr__(self) -> str:
        return (
            "DryCountPassEvidence("
            f"fold_id={self.fold_id!r}, family={self.family!r}, "
            f"config_id={self.config_id!r}, modeled_entries={self.modeled_entries}, "
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
    masked_id: int
    binding: tuple[str, str, str]
    counts_fingerprint: str
    vault_token: bytes


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
    payload = pickle.dumps(raw_value, protocol=5)
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
    """Issue PASS only for the exact bound counts and sealed threshold."""
    record = _record_for(masked)
    if type(dry_counts) is not bc.BlindCounts:
        raise TypeError("dry_counts must be an exact BlindCounts instance")
    if record.counts is not dry_counts:
        raise OOSMaskBypassError(
            "dry counts are not the actual object bound by H4 to this mask"
        )
    fingerprint = _counts_fingerprint(dry_counts)
    if fingerprint != record.counts_fingerprint:
        raise OOSMaskBypassError("bound dry counts changed after mask issuance")
    sealed_minimum = wf_seal.min_modeled_entries_per_fold()
    vault_token = _vault().call(("ISSUE", record.vault_handle, fingerprint))

    evidence = object.__new__(DryCountPassEvidence)
    for name, value in (
        ("_fold_id", record.binding[0]),
        ("_family", record.binding[1]),
        ("_config_id", record.binding[2]),
        ("_modeled_entries", dry_counts.modeled_entries_count),
        ("_min_modeled_entries_per_fold", sealed_minimum),
    ):
        object.__setattr__(evidence, name, value)
    evidence_id = id(evidence)
    evidence_ref = weakref.ref(evidence, _drop_evidence(evidence_id))
    _EVIDENCES[evidence_id] = _EvidenceRecord(
        evidence_ref=evidence_ref,
        masked_id=id(masked),
        binding=record.binding,
        counts_fingerprint=fingerprint,
        vault_token=vault_token,
    )
    return evidence


def unmask(masked: Masked, evidence: DryCountPassEvidence) -> Any:
    """One-shot reveal with registry-issued, matching PASS evidence."""
    record = _record_for(masked)
    if type(evidence) is not DryCountPassEvidence:
        raise TypeError("expected an exact DryCountPassEvidence instance")
    evidence_record = _EVIDENCES.get(id(evidence))
    if (
        evidence_record is None
        or evidence_record.evidence_ref() is not evidence
        or evidence_record.masked_id != id(masked)
        or evidence_record.binding != record.binding
        or evidence.binding_key() != record.binding
        or evidence_record.counts_fingerprint != record.counts_fingerprint
    ):
        raise OOSMaskBypassError(
            "PASS evidence was reconstructed, mutated, or issued for another mask"
        )
    payload = _vault().call(
        ("REVEAL", record.vault_handle, evidence_record.vault_token)
    )
    try:
        return pickle.loads(payload)
    finally:
        del payload
