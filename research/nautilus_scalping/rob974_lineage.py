"""ROB-974 immutable selected-universe projection and PIT lineage seal."""

from __future__ import annotations

from dataclasses import dataclass

import canonical_hash

PARENT_CONTENT_SHA256 = (
    "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
)
PARENT_MANIFEST_SHA256 = (
    "0767b44f976bf717cdc26bbcb0d01da1800418668f9f153461ce62486de10721"
)
WINDOW_START_ISO = "2025-07-01T00:00:00Z"
WINDOW_END_ISO = "2026-07-01T00:00:00Z"
SELECTED_UNIVERSE = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")


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

    def __post_init__(self) -> None:
        _sha(self.parent_content_sha256, "parent_content_sha256")
        _sha(self.parent_manifest_sha256, "parent_manifest_sha256")
        _sha(self.input_hash, "input_hash")
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

    @classmethod
    def create(
        cls, *, input_hash: str, context_start: int, context_end: int
    ) -> DerivedManifest:
        return cls(
            PARENT_CONTENT_SHA256,
            PARENT_MANIFEST_SHA256,
            input_hash,
            context_start,
            context_end,
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
        }

    @property
    def hash(self) -> str:
        return canonical_hash.canonical_sha256(self.to_dict())
