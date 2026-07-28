"""ROB-1064 H6 — immutable 16-config / 128-cell trial accounting seal.

This module is deliberately pure and offline.  It accepts already-produced
structural H4 evidence, validates it against an exact H2 config authority and
an exact eight-fold authority, and emits the sole report H5 may consume.
There is no broker, order, background-task, database, clock, filesystem, or
market data access here.

Failed and rejected configs remain first-class trials.  Missing configs/cells
are reported as missing; they are never synthesized with a convenient zero.
Status history is immutable and append-only: terminal states cannot be
rewritten, and corrections must be represented by a distinct run identity.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "EXPECTED_CELLS",
    "EXPECTED_CONFIGS",
    "TRIAL_STATUSES",
    "AccountingError",
    "AccountingReport",
    "AccountingSeal",
    "AuthorityError",
    "DuplicateCellError",
    "DuplicateTrialError",
    "ExpectedConfig",
    "FoldCell",
    "H5GateBlocked",
    "SealIntegrityError",
    "StatusEvent",
    "StatusTransitionError",
    "TrialProvenance",
    "TrialRecord",
    "append_status",
    "seal_trial_accounting",
    "verify_seal_for_h5",
]

SCHEMA_VERSION = "alpaca_track_h6_trial_accounting.v1"
EXPECTED_CONFIGS = 16
EXPECTED_FOLDS = 8
EXPECTED_CELLS = EXPECTED_CONFIGS * EXPECTED_FOLDS

TRIAL_STATUSES = (
    "registered",
    "executed",
    "insufficient_sample",
    "turnover_band_reject",
    "cost_cap_reject",
    "no_selected_config",
    "structural_incomplete",
    "scored",
)
_TERMINAL_STATUSES = frozenset(
    {
        "insufficient_sample",
        "turnover_band_reject",
        "cost_cap_reject",
        "no_selected_config",
        "structural_incomplete",
        "scored",
    }
)
_ALLOWED_TRANSITIONS = {
    "registered": frozenset(TRIAL_STATUSES[1:]),
    "executed": _TERMINAL_STATUSES,
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AccountingError(ValueError):
    """Base error for malformed or ambiguous H6 evidence."""


class AuthorityError(AccountingError):
    """The expected H2 config/fold authority is not exactly 16 x 8."""


class DuplicateTrialError(AccountingError):
    """Two input records claim the same strategy/config campaign slot."""


class DuplicateCellError(AccountingError):
    """Two fold cells claim the same strategy/config/fold slot."""


class StatusTransitionError(AccountingError):
    """A status history is non-append-only or rewrites a terminal state."""


class SealIntegrityError(AccountingError):
    """A supplied seal's report/hash does not match its committed evidence."""


class H5GateBlocked(AccountingError):
    """The verified seal is honest but not usable for H5 performance work."""


def _require_nonempty_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty built-in str")
    return value


def _require_hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise TypeError(f"{name} must be a lowercase SHA-256 hex string")
    return value


def _require_count(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a built-in int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_status(value: object, name: str = "status") -> str:
    if type(value) is not str or value not in TRIAL_STATUSES:
        raise ValueError(f"{name} must be one of {TRIAL_STATUSES!r}")
    return value


@dataclass(frozen=True, slots=True)
class ExpectedConfig:
    """One exact H2 config identity H6 must account for."""

    strategy: str
    config_id: str
    config_hash: str

    def __post_init__(self) -> None:
        _require_nonempty_string(self.strategy, "strategy")
        _require_nonempty_string(self.config_id, "config_id")
        _require_hex64(self.config_hash, "config_hash")

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy, self.config_id)

    def to_payload(self) -> dict[str, str]:
        return {
            "strategy": self.strategy,
            "config_id": self.config_id,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True, slots=True)
class TrialProvenance:
    """Exact non-config identity of one H4 invocation."""

    corpus_manifest_hash: str
    fold_schedule_hash: str
    code_hash: str
    run_id: str

    def __post_init__(self) -> None:
        _require_hex64(self.corpus_manifest_hash, "corpus_manifest_hash")
        _require_hex64(self.fold_schedule_hash, "fold_schedule_hash")
        _require_hex64(self.code_hash, "code_hash")
        _require_nonempty_string(self.run_id, "run_id")

    def to_payload(self) -> dict[str, str]:
        return {
            "corpus_manifest_hash": self.corpus_manifest_hash,
            "fold_schedule_hash": self.fold_schedule_hash,
            "code_hash": self.code_hash,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """One append-only config-level status transition."""

    sequence: int
    status: str
    reason: str | None

    def __post_init__(self) -> None:
        _require_count(self.sequence, "sequence")
        _require_status(self.status)
        if self.reason is not None:
            _require_nonempty_string(self.reason, "reason")
        if self.status == "structural_incomplete" and self.reason is None:
            raise ValueError("structural_incomplete status requires an explicit reason")

    def to_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class FoldCell:
    """One explicitly observed or explicitly unobserved config/fold cell."""

    strategy: str
    config_id: str
    fold_id: str
    status: str
    observation_count: int | None
    unobserved_reason: str | None

    def __post_init__(self) -> None:
        _require_nonempty_string(self.strategy, "strategy")
        _require_nonempty_string(self.config_id, "config_id")
        _require_nonempty_string(self.fold_id, "fold_id")
        _require_status(self.status)
        if self.observation_count is None:
            if self.unobserved_reason is None:
                raise ValueError(
                    "null observation_count requires an explicit unobserved_reason"
                )
            _require_nonempty_string(self.unobserved_reason, "unobserved_reason")
        else:
            _require_count(self.observation_count, "observation_count")
            if self.unobserved_reason is not None:
                raise ValueError(
                    "unobserved_reason must be null when observation_count is observed"
                )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.strategy, self.config_id, self.fold_id)

    def to_payload(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "config_id": self.config_id,
            "fold_id": self.fold_id,
            "status": self.status,
            "observation_count": self.observation_count,
            "unobserved_reason": self.unobserved_reason,
        }


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """One immutable config trial containing all supplied fold cells."""

    strategy: str
    config_id: str
    config_hash: str
    provenance: TrialProvenance
    primary: bool
    retry_count: int
    status_events: tuple[StatusEvent, ...]
    cells: tuple[FoldCell, ...]

    def __post_init__(self) -> None:
        _require_nonempty_string(self.strategy, "strategy")
        _require_nonempty_string(self.config_id, "config_id")
        _require_hex64(self.config_hash, "config_hash")
        if type(self.provenance) is not TrialProvenance:
            raise TypeError("provenance must be TrialProvenance")
        if type(self.primary) is not bool:
            raise TypeError("primary must be a built-in bool")
        _require_count(self.retry_count, "retry_count")
        if type(self.status_events) is not tuple or not self.status_events:
            raise TypeError("status_events must be a non-empty tuple")
        if type(self.cells) is not tuple:
            raise TypeError("cells must be a tuple")

        if self.status_events[0].sequence != 0:
            raise StatusTransitionError("status event sequence must start at zero")
        if self.status_events[0].status != "registered":
            raise StatusTransitionError("first status event must be registered")
        for expected_sequence, event in enumerate(self.status_events):
            if type(event) is not StatusEvent:
                raise TypeError("status_events must contain StatusEvent values")
            if event.sequence != expected_sequence:
                raise StatusTransitionError(
                    "status event sequence must be contiguous and append-only"
                )
            if expected_sequence:
                previous = self.status_events[expected_sequence - 1].status
                allowed = _ALLOWED_TRANSITIONS.get(previous, frozenset())
                if event.status not in allowed:
                    terminal = " terminal" if previous in _TERMINAL_STATUSES else ""
                    raise StatusTransitionError(
                        f"cannot overwrite{terminal} status {previous!r} "
                        f"with {event.status!r}"
                    )

        for cell in self.cells:
            if type(cell) is not FoldCell:
                raise TypeError("cells must contain FoldCell values")
            if (cell.strategy, cell.config_id) != self.key:
                raise AccountingError(
                    "fold cell strategy/config_id must match its trial record"
                )

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy, self.config_id)

    @property
    def identity(self) -> tuple[str, ...]:
        return (
            self.strategy,
            self.config_id,
            self.config_hash,
            self.provenance.corpus_manifest_hash,
            self.provenance.fold_schedule_hash,
            self.provenance.code_hash,
            self.provenance.run_id,
        )

    @property
    def current_status(self) -> str:
        return self.status_events[-1].status

    def to_payload(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "config_id": self.config_id,
            "config_hash": self.config_hash,
            "provenance": self.provenance.to_payload(),
            "primary": self.primary,
            "retry_count": self.retry_count,
            "status_events": [event.to_payload() for event in self.status_events],
            "cells": [
                cell.to_payload()
                for cell in sorted(self.cells, key=lambda value: value.key)
            ],
        }


def append_status(
    trial: TrialRecord,
    *,
    status: str,
    reason: str | None,
) -> TrialRecord:
    """Return a new record with one appended event; never edit prior events."""

    if type(trial) is not TrialRecord:
        raise TypeError("trial must be TrialRecord")
    event = StatusEvent(
        sequence=len(trial.status_events),
        status=status,
        reason=reason,
    )
    return replace(trial, status_events=(*trial.status_events, event))


@dataclass(frozen=True, slots=True)
class AccountingReport:
    expected: int
    registered: int
    primary: int
    status_sum: int
    cells: int
    retry: int
    performance_usable: bool
    structural_incomplete: int
    status_counts: Mapping[str, int]
    missing_config_ids: tuple[str, ...]
    extra_config_ids: tuple[str, ...]
    missing_cell_ids: tuple[str, ...]
    extra_cell_ids: tuple[str, ...]
    violations: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "expected",
            "registered",
            "primary",
            "status_sum",
            "cells",
            "retry",
            "structural_incomplete",
        ):
            _require_count(getattr(self, name), name)
        if type(self.performance_usable) is not bool:
            raise TypeError("performance_usable must be a built-in bool")
        counts = dict(self.status_counts)
        if set(counts) != set(TRIAL_STATUSES):
            raise ValueError("status_counts must report every trial status")
        for status, count in counts.items():
            _require_status(status, "status_counts key")
            _require_count(count, f"status_counts[{status!r}]")
        object.__setattr__(
            self,
            "status_counts",
            MappingProxyType({key: counts[key] for key in TRIAL_STATUSES}),
        )
        for name in (
            "missing_config_ids",
            "extra_config_ids",
            "missing_cell_ids",
            "extra_cell_ids",
            "violations",
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(
                type(value) is not str for value in values
            ):
                raise TypeError(f"{name} must be a tuple of built-in str values")

    def to_payload(self) -> dict[str, object]:
        return {
            "expected": self.expected,
            "registered": self.registered,
            "primary": self.primary,
            "status_sum": self.status_sum,
            "cells": self.cells,
            "retry": self.retry,
            "performance_usable": self.performance_usable,
            "structural_incomplete": self.structural_incomplete,
            "status_counts": dict(self.status_counts),
            "missing_config_ids": list(self.missing_config_ids),
            "extra_config_ids": list(self.extra_config_ids),
            "missing_cell_ids": list(self.missing_cell_ids),
            "extra_cell_ids": list(self.extra_cell_ids),
            "violations": list(self.violations),
        }


@dataclass(frozen=True, slots=True)
class AccountingSeal:
    report: AccountingReport
    trials: tuple[TrialRecord, ...]
    expected_configs: tuple[ExpectedConfig, ...]
    expected_fold_ids: tuple[str, ...]
    semantic_hash: str

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "semantic_hash": self.semantic_hash,
            "report": self.report.to_payload(),
            "expected_configs": [
                config.to_payload() for config in self.expected_configs
            ],
            "expected_fold_ids": list(self.expected_fold_ids),
            "trials": [trial.to_payload() for trial in self.trials],
        }

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_payload(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )


def _format_config_key(key: tuple[str, str]) -> str:
    return "/".join(key)


def _format_cell_key(key: tuple[str, str, str]) -> str:
    return "/".join(key)


def _canonical_authority(
    expected_configs: Sequence[ExpectedConfig],
    expected_fold_ids: Sequence[str],
) -> tuple[tuple[ExpectedConfig, ...], tuple[str, ...]]:
    configs = tuple(expected_configs)
    if len(configs) != EXPECTED_CONFIGS:
        raise AuthorityError(
            f"expected authority must contain exactly {EXPECTED_CONFIGS} configs"
        )
    if any(type(config) is not ExpectedConfig for config in configs):
        raise TypeError("expected_configs must contain ExpectedConfig values")
    if len({config.key for config in configs}) != EXPECTED_CONFIGS:
        raise AuthorityError("expected config authority contains duplicate identities")
    if len({config.config_hash for config in configs}) != EXPECTED_CONFIGS:
        raise AuthorityError("expected config authority contains duplicate hashes")

    folds = tuple(expected_fold_ids)
    if len(folds) != EXPECTED_FOLDS:
        raise AuthorityError(
            f"expected fold authority must contain exactly {EXPECTED_FOLDS} folds"
        )
    for fold_id in folds:
        _require_nonempty_string(fold_id, "fold_id")
    if len(set(folds)) != EXPECTED_FOLDS:
        raise AuthorityError("expected fold authority contains duplicate fold ids")
    return (
        tuple(sorted(configs, key=lambda config: config.key)),
        tuple(sorted(folds)),
    )


def seal_trial_accounting(
    trials: Sequence[TrialRecord],
    *,
    expected_configs: Sequence[ExpectedConfig],
    expected_fold_ids: Sequence[str],
) -> AccountingSeal:
    """Validate and seal complete or incomplete campaign accounting.

    Shape failures that can be represented honestly (15/17 configs,
    127/129 cells, retries, structural incompleteness) produce a sealed
    ``performance_usable=false`` report.  Ambiguous evidence (duplicate
    trial/cell identity) raises instead of silently merging records.
    """

    configs, folds = _canonical_authority(expected_configs, expected_fold_ids)
    trial_values = tuple(trials)
    if any(type(trial) is not TrialRecord for trial in trial_values):
        raise TypeError("trials must contain TrialRecord values")

    trial_keys = [trial.key for trial in trial_values]
    duplicate_trial_keys = sorted(
        key for key, count in Counter(trial_keys).items() if count > 1
    )
    if duplicate_trial_keys:
        raise DuplicateTrialError(
            "duplicate trial identity: "
            + ", ".join(_format_config_key(key) for key in duplicate_trial_keys)
        )

    actual_cell_keys: list[tuple[str, str, str]] = []
    for trial in trial_values:
        cell_keys = [cell.key for cell in trial.cells]
        duplicates = sorted(
            key for key, count in Counter(cell_keys).items() if count > 1
        )
        if duplicates:
            raise DuplicateCellError(
                "duplicate fold cell: "
                + ", ".join(_format_cell_key(key) for key in duplicates)
            )
        actual_cell_keys.extend(cell_keys)

    expected_by_key = {config.key: config for config in configs}
    expected_config_keys = set(expected_by_key)
    actual_config_keys = set(trial_keys)
    missing_config_keys = sorted(expected_config_keys - actual_config_keys)
    extra_config_keys = sorted(actual_config_keys - expected_config_keys)

    expected_cell_keys = {
        (*config.key, fold_id) for config in configs for fold_id in folds
    }
    actual_cell_key_set = set(actual_cell_keys)
    missing_cell_keys = sorted(expected_cell_keys - actual_cell_key_set)
    extra_cell_keys = sorted(actual_cell_key_set - expected_cell_keys)

    config_hash_mismatches = sorted(
        trial.key
        for trial in trial_values
        if trial.key in expected_by_key
        and trial.config_hash != expected_by_key[trial.key].config_hash
    )
    provenance_identities = {
        (
            trial.provenance.corpus_manifest_hash,
            trial.provenance.fold_schedule_hash,
            trial.provenance.code_hash,
            trial.provenance.run_id,
        )
        for trial in trial_values
    }
    hidden_structural_cells = sorted(
        trial.key
        for trial in trial_values
        if trial.current_status != "structural_incomplete"
        and any(cell.status == "structural_incomplete" for cell in trial.cells)
    )

    registered = len(trial_values)
    primary = sum(1 for trial in trial_values if trial.primary)
    status_counts_counter = Counter(trial.current_status for trial in trial_values)
    status_counts = {
        status: status_counts_counter.get(status, 0) for status in TRIAL_STATUSES
    }
    status_sum = sum(status_counts.values())
    cells = sum(len(trial.cells) for trial in trial_values)
    retry = sum(trial.retry_count for trial in trial_values)
    structural_incomplete = status_counts["structural_incomplete"]

    violations: list[str] = []
    if registered != EXPECTED_CONFIGS:
        violations.append("registered_count_not_16")
    if primary != EXPECTED_CONFIGS:
        violations.append("primary_count_not_16")
    if status_sum != EXPECTED_CONFIGS:
        violations.append("status_sum_not_16")
    if cells != EXPECTED_CELLS:
        violations.append("cell_count_not_128")
    if retry != 0:
        violations.append("retry_count_nonzero")
    if missing_config_keys:
        violations.append("missing_configs")
    if extra_config_keys:
        violations.append("unexpected_configs")
    if missing_cell_keys:
        violations.append("missing_cells")
    if extra_cell_keys:
        violations.append("unexpected_cells")
    if config_hash_mismatches:
        violations.append("config_hash_mismatch")
    if len(provenance_identities) > 1:
        violations.append("mixed_provenance")
    if structural_incomplete:
        violations.append("structural_incomplete")
    if hidden_structural_cells:
        violations.append("structural_cell_hidden")

    report = AccountingReport(
        expected=EXPECTED_CONFIGS,
        registered=registered,
        primary=primary,
        status_sum=status_sum,
        cells=cells,
        retry=retry,
        performance_usable=not violations,
        structural_incomplete=structural_incomplete,
        status_counts=status_counts,
        missing_config_ids=tuple(
            _format_config_key(key) for key in missing_config_keys
        ),
        extra_config_ids=tuple(_format_config_key(key) for key in extra_config_keys),
        missing_cell_ids=tuple(_format_cell_key(key) for key in missing_cell_keys),
        extra_cell_ids=tuple(_format_cell_key(key) for key in extra_cell_keys),
        violations=tuple(violations),
    )

    canonical_trials = tuple(sorted(trial_values, key=lambda trial: trial.key))
    semantic_payload = {
        "schema_version": SCHEMA_VERSION,
        "expected_configs": [config.to_payload() for config in configs],
        "expected_fold_ids": list(folds),
        "trials": [trial.to_payload() for trial in canonical_trials],
    }
    return AccountingSeal(
        report=report,
        trials=canonical_trials,
        expected_configs=configs,
        expected_fold_ids=folds,
        semantic_hash=canonical_sha256(semantic_payload),
    )


def verify_seal_for_h5(seal: AccountingSeal) -> AccountingReport:
    """Verify an H6 seal and expose only its already-derived H5 gate report.

    H5 calls this boundary; it does not count configs/cells or derive
    ``performance_usable`` itself.  The H6 verifier recomputes its own sealed
    evidence internally, rejects any hash/report drift, then fails closed if
    the verified report is not performance-usable.
    """

    if type(seal) is not AccountingSeal:
        raise TypeError("seal must be AccountingSeal")
    rebuilt = seal_trial_accounting(
        seal.trials,
        expected_configs=seal.expected_configs,
        expected_fold_ids=seal.expected_fold_ids,
    )
    if (
        rebuilt.semantic_hash != seal.semantic_hash
        or rebuilt.report.to_payload() != seal.report.to_payload()
    ):
        raise SealIntegrityError("accounting seal hash/report integrity check failed")
    if not rebuilt.report.performance_usable:
        raise H5GateBlocked(
            "verified accounting seal is not performance_usable; "
            "H5 must mark the whole campaign incomplete"
        )
    return rebuilt.report
