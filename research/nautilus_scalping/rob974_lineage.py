"""ROB-974 immutable selected-universe projection and PIT lineage seal."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import canonical_hash
from rob941_manifest import CorpusManifest

PARENT_CONTENT_SHA256 = (
    "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
)
PARENT_MANIFEST_SHA256 = (
    "0767b44f976bf717cdc26bbcb0d01da1800418668f9f153461ce62486de10721"
)
WINDOW_START_ISO = "2025-07-01T00:00:00Z"
WINDOW_END_ISO = "2026-07-01T00:00:00Z"
SELECTED_UNIVERSE = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
GENERATOR_VERSION = "rob974-h1-v2"
_PARENT_MANIFEST_PATH = (
    Path(__file__).parent / "data_manifests/rob941_corpus_manifest.v1.json"
)


def _sha(value: str, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


def verify_parent(path: Path = _PARENT_MANIFEST_PATH) -> CorpusManifest:
    """Load and independently verify the frozen parent before derived evidence."""
    physical = hashlib.sha256(path.read_bytes()).hexdigest()
    if physical != PARENT_MANIFEST_SHA256:
        raise ValueError("parent manifest physical SHA-256 mismatch")
    manifest = CorpusManifest.load(path)
    if manifest.content_hash() != PARENT_CONTENT_SHA256:
        raise ValueError("parent manifest canonical content hash mismatch")
    return manifest


def feature_input_hash(rows: object) -> str:
    """Typed seal over actual selected minute inputs, never a caller assertion."""
    if not isinstance(rows, Mapping) or set(rows) != set(SELECTED_UNIVERSE):
        raise ValueError("rows must cover the exact selected universe")
    payload: dict[str, list[dict[str, object]]] = {}
    for symbol in SELECTED_UNIVERSE:
        values = rows[symbol]
        if not isinstance(values, Sequence):
            raise TypeError("minute rows must be a sequence")
        payload[symbol] = [value.__dict__.copy() for value in values]
    return canonical_hash.canonical_sha256(payload)


@dataclass(frozen=True)
class DerivedManifest:
    """Canonical, pure evidence only; it does not materialize or mutate corpus data."""

    parent_content_sha256: str
    parent_manifest_sha256: str
    input_hash: str
    context_start: int
    context_end: int
    universe: tuple[str, ...] = SELECTED_UNIVERSE
    window_start_iso: str = WINDOW_START_ISO
    window_end_iso: str = WINDOW_END_ISO
    schema_version: str = "rob974-h1-v1"
    funding_authority: str = "rob941 PIT funding sidecar projection only"
    generator_version: str = GENERATOR_VERSION
    funding_source_sha256: str = PARENT_CONTENT_SHA256
    funding_coverage: tuple[tuple[str, int, int | None, int | None], ...] = ()
    eligibility: tuple[tuple[str, bool, bool, str | None], ...] = ()
    feature_hash: str | None = None

    def __post_init__(self) -> None:
        _sha(self.parent_content_sha256, "parent_content_sha256")
        _sha(self.parent_manifest_sha256, "parent_manifest_sha256")
        _sha(self.input_hash, "input_hash")
        _sha(self.funding_source_sha256, "funding_source_sha256")
        if self.feature_hash is not None:
            _sha(self.feature_hash, "feature_hash")
        _int(self.context_start, "context_start")
        _int(self.context_end, "context_end")
        if self.context_end < self.context_start:
            raise ValueError("context range reversed")
        if self.universe != SELECTED_UNIVERSE:
            raise ValueError("selected universe/order is frozen")
        if (self.window_start_iso, self.window_end_iso) != (
            WINDOW_START_ISO,
            WINDOW_END_ISO,
        ):
            raise ValueError("source window is frozen")
        if type(self.generator_version) is not str or not self.generator_version:
            raise TypeError("generator_version must be non-empty str")
        if (
            self.funding_coverage
            and tuple(item[0] for item in self.funding_coverage) != SELECTED_UNIVERSE
        ):
            raise ValueError("funding coverage must use selected order")
        if (
            self.eligibility
            and tuple(item[0] for item in self.eligibility) != SELECTED_UNIVERSE
        ):
            raise ValueError("eligibility must use selected order")

    @classmethod
    def create(
        cls,
        *,
        input_hash: str | None = None,
        rows: object | None = None,
        context_start: int,
        context_end: int,
        funding_coverage: tuple[tuple[str, int, int | None, int | None], ...] = (),
        funding_source_sha256: str = PARENT_CONTENT_SHA256,
        feature_hash: str | None = None,
    ) -> DerivedManifest:
        parent = verify_parent()
        if rows is not None:
            computed = feature_input_hash(rows)
            if input_hash is not None and input_hash != computed:
                raise ValueError("input_hash does not match selected minute rows")
            input_hash = computed
        if input_hash is None:
            raise ValueError("rows or an explicit input_hash is required")
        eligibility_by_symbol = {entry.symbol: entry for entry in parent.eligibility}
        eligibility = tuple(
            (
                symbol,
                eligibility_by_symbol[symbol].historical_only,
                eligibility_by_symbol[symbol].demo_execution_eligible,
                eligibility_by_symbol[symbol].reason,
            )
            for symbol in SELECTED_UNIVERSE
        )
        return cls(
            PARENT_CONTENT_SHA256,
            PARENT_MANIFEST_SHA256,
            input_hash,
            context_start,
            context_end,
            funding_coverage=funding_coverage,
            funding_source_sha256=funding_source_sha256,
            eligibility=eligibility,
            feature_hash=feature_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "parent_content_sha256": self.parent_content_sha256,
            "parent_manifest_sha256": self.parent_manifest_sha256,
            "window_start_iso": self.window_start_iso,
            "window_end_iso": self.window_end_iso,
            "universe": list(self.universe),
            "input_hash": self.input_hash,
            "context_start": self.context_start,
            "context_end": self.context_end,
            "contracts": {
                "bars": "UTC complete-only 4h",
                "vwap": "12h/24h contiguous minute typical-price",
                "gap": "reset",
                "pit": "raw completed past only",
            },
            "funding_authority": self.funding_authority,
            "generator_version": self.generator_version,
            "funding_source_sha256": self.funding_source_sha256,
            "funding_coverage": [list(item) for item in self.funding_coverage],
            "eligibility": [list(item) for item in self.eligibility],
            "feature_hash": self.feature_hash,
        }

    @property
    def hash(self) -> str:
        return canonical_hash.canonical_sha256(self.to_dict())
