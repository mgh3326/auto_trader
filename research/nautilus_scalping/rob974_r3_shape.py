"""Code-issued campaign shapes for the frozen ROB-974 R2/R3 lineages.

The public R2 exact-48 modules remain untouched.  This additive seam gives
new callers only two issued values and rejects value-equal caller forgeries.
R3 mapping validation has its own exact-12 name and error taxonomy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "R2_CANONICAL_ROW_ORDER",
    "R2_SHAPE",
    "R3_CANONICAL_ROW_ORDER",
    "R3_SHAPE",
    "CampaignShapeError",
    "CampaignShapeForgeryError",
    "Exact12MappingError",
    "compute_exact_12_mapping_hash",
    "require_issued_shape",
]

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ISSUED_SHAPE_SEAL = object()

R2_CANONICAL_ROW_ORDER: tuple[str, ...] = tuple(
    [f"S3-{index:02d}" for index in range(24)]
    + [f"S4-{index:02d}" for index in range(24)]
)
R3_CANONICAL_ROW_ORDER: tuple[str, ...] = tuple(
    [f"S3-R3-{index:02d}" for index in range(3)]
    + [f"S4-R3-{index:02d}" for index in range(9)]
)


class CampaignShapeError(ValueError):
    """A campaign shape is malformed or outside the issued roster."""


class CampaignShapeForgeryError(CampaignShapeError):
    """A value-equal but non-issued shape was supplied by a caller."""


class Exact12MappingError(CampaignShapeError):
    """The R3 row/experiment mapping is not the literal exact-12 roster."""


@dataclass(frozen=True, slots=True)
class _CampaignShape:
    lineage: str
    family_counts: tuple[tuple[str, int], tuple[str, int]]
    row_order: tuple[str, ...]
    total_rows: int
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _ISSUED_SHAPE_SEAL:
            raise CampaignShapeForgeryError("campaign shape was not code-issued")
        if type(self.row_order) is not tuple or type(self.family_counts) is not tuple:
            raise CampaignShapeError("issued shape containers must be exact tuples")
        if sum(count for _slug, count in self.family_counts) != self.total_rows:
            raise CampaignShapeError("issued shape family counts do not reconcile")
        if len(self.row_order) != self.total_rows:
            raise CampaignShapeError("issued shape row order does not reconcile")


R2_SHAPE = _CampaignShape(
    lineage="ROB-974-R2",
    family_counts=(("S3", 24), ("S4", 24)),
    row_order=R2_CANONICAL_ROW_ORDER,
    total_rows=48,
    _seal=_ISSUED_SHAPE_SEAL,
)
R3_SHAPE = _CampaignShape(
    lineage="ROB-974-R3",
    family_counts=(("S3", 3), ("S4", 9)),
    row_order=R3_CANONICAL_ROW_ORDER,
    total_rows=12,
    _seal=_ISSUED_SHAPE_SEAL,
)


def require_issued_shape(shape: object) -> _CampaignShape:
    """Accept only the two singleton values issued above.

    Checking singleton identity, not merely dataclass equality or the private
    seal value, rejects ``dataclasses.replace(R3_SHAPE)`` and other forged
    shapes even if every visible field is identical.
    """

    if shape is not R2_SHAPE and shape is not R3_SHAPE:
        raise CampaignShapeForgeryError(
            "campaign shape must be the issued R2_SHAPE or R3_SHAPE singleton"
        )
    return shape


def compute_exact_12_mapping_hash(
    mapping: tuple[tuple[str, str], ...],
) -> str:
    """Seal the literal ordered R3 mapping after full structural validation."""

    if type(mapping) is not tuple:
        raise Exact12MappingError("exact-12 mapping must be an exact tuple")
    if len(mapping) != R3_SHAPE.total_rows:
        raise Exact12MappingError("exact-12 mapping must contain exactly 12 entries")
    for item in mapping:
        if type(item) is not tuple or len(item) != 2:
            raise Exact12MappingError("each exact-12 entry must be an exact 2-tuple")
        row_id, experiment_id = item
        if type(row_id) is not str or type(experiment_id) is not str:
            raise Exact12MappingError("exact-12 row and experiment IDs must be str")
        if _HEX64_RE.fullmatch(experiment_id) is None:
            raise Exact12MappingError(
                f"experiment ID for {row_id!r} must be lowercase 64-hex"
            )
    row_ids = tuple(row_id for row_id, _experiment_id in mapping)
    if row_ids != R3_CANONICAL_ROW_ORDER:
        raise Exact12MappingError(
            "mapping order must be literal S3-R3-00..02,S4-R3-00..08"
        )
    experiment_ids = tuple(experiment_id for _row_id, experiment_id in mapping)
    if len(set(experiment_ids)) != R3_SHAPE.total_rows:
        raise Exact12MappingError("exact-12 experiment IDs must be unique")
    return canonical_sha256(dict(mapping))
