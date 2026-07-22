"""Frozen ROB-974 R3 exact-12 manifest and relaxation-graph authority."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

from rob974_r3_shape import R3_CANONICAL_ROW_ORDER

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "FROZEN_R3_ROSTER",
    "FROZEN_R3_S3_CONFIGS",
    "FROZEN_R3_S4_CONFIGS",
    "PREREGISTRATION_DOCUMENT_SHA256",
    "R3_ADJACENCY_EDGES",
    "R3_MANIFEST_CONTRACT_HASH",
    "R3_MANIFEST_CONTRACT_VERSION",
    "R3_RELAXATION_RAYS",
    "R3_S3_GATE_CONTRACT_VERSION",
    "R3_S3_STRATEGY_CONTRACT",
    "R3_S4_GATE_CONTRACT_VERSION",
    "R3_S4_STRATEGY_CONTRACT",
    "R3ManifestError",
    "R3RelaxationRay",
    "R3S3Config",
    "R3S4Config",
    "R3StrategyContract",
    "S3_EXCLUDED_CELLS",
    "S3_R2_ANCHOR",
    "S4_DUPLICATE_D_MIN_BP_LT",
    "S4_EXCLUDED_CELLS",
    "S4_R2_ANCHOR",
    "S4_SATURATION_Z_LT",
    "assert_registered_r3_config",
    "get_r3_config",
    "r3_manifest_contract_payload",
    "validate_r2_anchor_projection",
    "validate_r3_manifest",
    "validate_r3_relaxation_rays",
]

PREREGISTRATION_DOCUMENT_SHA256 = (
    "b2f03a23285945c8fda84c56a040fe2466541e8250e0b01ea987ba9d315e7ac5"
)
R2_RESEARCH_DOCUMENT_SHA256 = (
    "2f535196cf0f0a03292e8f4c1806794ffbf8282ba7b5c3f564a930763577a009"
)
R3_MANIFEST_CONTRACT_VERSION = "rob974_r3_manifest.v1"
R3_S3_GATE_CONTRACT_VERSION = "rob974_r3_s3_gate.v1"
R3_S4_GATE_CONTRACT_VERSION = "rob974_r3_s4_gate.v1"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PLANNING_CLASSES = frozenset({"boundary", "power"})


class R3ManifestError(ValueError):
    """R3 roster, anchor, exclusion, or graph differs from preregistration."""


def _str(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise R3ManifestError(f"{name} must be an exact non-empty str")
    return value


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise R3ManifestError(f"{name} must be an exact int")
    return value


def _float(value: object, name: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise R3ManifestError(f"{name} must be an exact finite float")
    return value


def _hex64(value: object, name: str) -> str:
    if type(value) is not str or _HEX64_RE.fullmatch(value) is None:
        raise R3ManifestError(f"{name} must be lowercase 64-hex")
    return value


@dataclass(frozen=True, slots=True)
class S3Anchor:
    r2_config_id: str
    L: int
    q_min: float
    ER_min: float
    k_SL: float
    R_TP: float

    def as_tuple(self) -> tuple[str, int, float, float, float, float]:
        return (
            self.r2_config_id,
            self.L,
            self.q_min,
            self.ER_min,
            self.k_SL,
            self.R_TP,
        )


@dataclass(frozen=True, slots=True)
class S4Anchor:
    r2_config_id: str
    W: int
    k_SL: float
    R_TP: float

    def as_tuple(self) -> tuple[str, int, float, float]:
        return self.r2_config_id, self.W, self.k_SL, self.R_TP


S3_R2_ANCHOR = S3Anchor("S3-03", 16, 0.35, 0.35, 1.25, 1.60)
S4_R2_ANCHOR = S4Anchor("S4-02", 150, 1.25, 1.50)
S3_EXCLUDED_CELLS: tuple[tuple[float, int], ...] = ((0.05, 25), (0.10, 0))
S4_EXCLUDED_CELLS: tuple[tuple[float, int], ...] = ((1.20, 140), (1.00, 180))
S4_SATURATION_Z_LT = 0.60
S4_DUPLICATE_D_MIN_BP_LT = 140


@dataclass(frozen=True, slots=True)
class R3S3Config:
    config_id: str
    L: int
    q_min: float
    ER_min: float
    k_SL: float
    R_TP: float
    S_min: float
    M_min_bp: int
    planning_class: str

    def __post_init__(self) -> None:
        _str(self.config_id, "config_id")
        _int(self.L, "L")
        for name in ("q_min", "ER_min", "k_SL", "R_TP", "S_min"):
            if _float(getattr(self, name), name) < 0.0:
                raise R3ManifestError(f"{name} must be non-negative")
        if _int(self.M_min_bp, "M_min_bp") < 0:
            raise R3ManifestError("M_min_bp must be non-negative")
        if self.planning_class not in _PLANNING_CLASSES:
            raise R3ManifestError("planning_class is outside the closed set")


@dataclass(frozen=True, slots=True)
class R3S4Config:
    config_id: str
    W: int
    z_entry: float
    d_min_bp: int
    k_SL: float
    R_TP: float
    planning_class: str

    def __post_init__(self) -> None:
        _str(self.config_id, "config_id")
        _int(self.W, "W")
        for name in ("z_entry", "k_SL", "R_TP"):
            if _float(getattr(self, name), name) < 0.0:
                raise R3ManifestError(f"{name} must be non-negative")
        if _int(self.d_min_bp, "d_min_bp") < 0:
            raise R3ManifestError("d_min_bp must be non-negative")
        if self.planning_class not in _PLANNING_CLASSES:
            raise R3ManifestError("planning_class is outside the closed set")


FROZEN_R3_ROSTER: tuple[R3S3Config | R3S4Config, ...] = (
    R3S3Config("S3-R3-00", 16, 0.35, 0.35, 1.25, 1.60, 0.05, 0, "boundary"),
    R3S3Config("S3-R3-01", 16, 0.35, 0.35, 1.25, 1.60, 0.00, 25, "boundary"),
    R3S3Config("S3-R3-02", 16, 0.35, 0.35, 1.25, 1.60, 0.00, 0, "boundary"),
    R3S4Config("S4-R3-00", 150, 1.10, 140, 1.25, 1.50, "boundary"),
    R3S4Config("S4-R3-01", 150, 1.00, 160, 1.25, 1.50, "boundary"),
    R3S4Config("S4-R3-02", 150, 1.00, 140, 1.25, 1.50, "boundary"),
    R3S4Config("S4-R3-03", 150, 0.80, 180, 1.25, 1.50, "boundary"),
    R3S4Config("S4-R3-04", 150, 0.80, 160, 1.25, 1.50, "boundary"),
    R3S4Config("S4-R3-05", 150, 0.80, 140, 1.25, 1.50, "power"),
    R3S4Config("S4-R3-06", 150, 0.60, 180, 1.25, 1.50, "boundary"),
    R3S4Config("S4-R3-07", 150, 0.60, 160, 1.25, 1.50, "power"),
    R3S4Config("S4-R3-08", 150, 0.60, 140, 1.25, 1.50, "power"),
)
FROZEN_R3_S3_CONFIGS: tuple[R3S3Config, ...] = FROZEN_R3_ROSTER[:3]  # type: ignore[assignment]
FROZEN_R3_S4_CONFIGS: tuple[R3S4Config, ...] = FROZEN_R3_ROSTER[3:]  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class R3RelaxationRay:
    ray_id: str
    family: str
    axis: str
    config_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _str(self.ray_id, "ray_id")
        if self.family not in ("S3", "S4"):
            raise R3ManifestError("ray family must be S3 or S4")
        allowed_axes = (
            ("S_min", "M_min_bp")
            if self.family == "S3"
            else ("z_entry", "d_min_bp")
        )
        if self.axis not in allowed_axes:
            raise R3ManifestError("ray axis differs from its family")
        if (
            type(self.config_ids) is not tuple
            or len(self.config_ids) < 2
            or any(type(item) is not str for item in self.config_ids)
            or len(set(self.config_ids)) != len(self.config_ids)
        ):
            raise R3ManifestError("ray config IDs must be a unique exact tuple")


R3_RELAXATION_RAYS: tuple[R3RelaxationRay, ...] = (
    R3RelaxationRay("S3-S-M0", "S3", "S_min", ("S3-R3-00", "S3-R3-02")),
    R3RelaxationRay("S3-M-S0", "S3", "M_min_bp", ("S3-R3-01", "S3-R3-02")),
    R3RelaxationRay(
        "S4-Z-D140",
        "S4",
        "z_entry",
        ("S4-R3-00", "S4-R3-02", "S4-R3-05", "S4-R3-08"),
    ),
    R3RelaxationRay(
        "S4-Z-D160",
        "S4",
        "z_entry",
        ("S4-R3-01", "S4-R3-04", "S4-R3-07"),
    ),
    R3RelaxationRay("S4-Z-D180", "S4", "z_entry", ("S4-R3-03", "S4-R3-06")),
    R3RelaxationRay("S4-D-Z1.0", "S4", "d_min_bp", ("S4-R3-01", "S4-R3-02")),
    R3RelaxationRay(
        "S4-D-Z0.8",
        "S4",
        "d_min_bp",
        ("S4-R3-03", "S4-R3-04", "S4-R3-05"),
    ),
    R3RelaxationRay(
        "S4-D-Z0.6",
        "S4",
        "d_min_bp",
        ("S4-R3-06", "S4-R3-07", "S4-R3-08"),
    ),
)
R3_ADJACENCY_EDGES: tuple[tuple[str, str], ...] = tuple(
    edge
    for ray in R3_RELAXATION_RAYS
    for edge in zip(ray.config_ids[:-1], ray.config_ids[1:], strict=True)
)


def validate_r3_manifest(rows: tuple[R3S3Config | R3S4Config, ...]) -> None:
    if type(rows) is not tuple or len(rows) != 12:
        raise R3ManifestError("R3 manifest must be one exact 12-row tuple")
    if tuple(row.config_id for row in rows) != R3_CANONICAL_ROW_ORDER:
        raise R3ManifestError("R3 manifest row IDs are missing, renamed, or reordered")
    if any(type(row) is not R3S3Config for row in rows[:3]) or any(
        type(row) is not R3S4Config for row in rows[3:]
    ):
        raise R3ManifestError("R3 manifest strategy split must use exact 3+9 DTOs")
    for row in rows[:3]:
        assert type(row) is R3S3Config
        if (row.S_min, row.M_min_bp) in S3_EXCLUDED_CELLS:
            raise R3ManifestError("R3 S3 manifest contains a preregistered exclusion")
        if (
            row.L,
            row.q_min,
            row.ER_min,
            row.k_SL,
            row.R_TP,
        ) != S3_R2_ANCHOR.as_tuple()[1:]:
            raise R3ManifestError("R3 S3 fixed R2 anchor fields drifted")
    for row in rows[3:]:
        assert type(row) is R3S4Config
        if (row.z_entry, row.d_min_bp) in S4_EXCLUDED_CELLS:
            raise R3ManifestError("R3 S4 manifest contains a preregistered exclusion")
        if row.z_entry < S4_SATURATION_Z_LT:
            raise R3ManifestError("R3 S4 manifest enters the preregistered saturation zone")
        if row.d_min_bp < S4_DUPLICATE_D_MIN_BP_LT:
            raise R3ManifestError("R3 S4 manifest enters the duplicate d_min zone")
        if (row.W, row.k_SL, row.R_TP) != S4_R2_ANCHOR.as_tuple()[1:]:
            raise R3ManifestError("R3 S4 fixed R2 anchor fields drifted")
    if rows != FROZEN_R3_ROSTER:
        raise R3ManifestError("R3 manifest differs from the frozen exact roster")


def validate_r3_relaxation_rays(rays: tuple[R3RelaxationRay, ...]) -> None:
    if type(rays) is not tuple or any(type(ray) is not R3RelaxationRay for ray in rays):
        raise R3ManifestError("relaxation rays must use one exact tuple authority")
    by_id = {row.config_id: row for row in FROZEN_R3_ROSTER}
    for ray in rays:
        try:
            configs = tuple(by_id[config_id] for config_id in ray.config_ids)
        except KeyError as exc:
            raise R3ManifestError("relaxation ray contains an unknown config") from exc
        expected_type = R3S3Config if ray.family == "S3" else R3S4Config
        if any(type(config) is not expected_type for config in configs):
            raise R3ManifestError("relaxation ray crosses strategy families")
        moving = tuple(getattr(config, ray.axis) for config in configs)
        if any(
            right >= left
            for left, right in zip(moving[:-1], moving[1:], strict=True)
        ):
            raise R3ManifestError("relaxation ray must be strictly loosening")
        fixed_fields = (
            ("M_min_bp",) if ray.axis == "S_min" else ("S_min",)
        ) if ray.family == "S3" else (
            ("d_min_bp",) if ray.axis == "z_entry" else ("z_entry",)
        )
        for field_name in fixed_fields:
            if len({getattr(config, field_name) for config in configs}) != 1:
                raise R3ManifestError("relaxation ray changes a fixed axis")
    if rays != R3_RELAXATION_RAYS:
        raise R3ManifestError("relaxation ray roster/order differs")
    edges = tuple(
        edge
        for ray in rays
        for edge in zip(ray.config_ids[:-1], ray.config_ids[1:], strict=True)
    )
    if len(edges) != len(set(edges)):
        raise R3ManifestError("relaxation graph contains duplicate directed edges")


def validate_r2_anchor_projection() -> None:
    """Assert the frozen R3 secondary fields still equal their R2 authorities."""

    from rob974_h3_manifest import get_config

    s3 = get_config(S3_R2_ANCHOR.r2_config_id)
    s4 = get_config(S4_R2_ANCHOR.r2_config_id)
    observed_s3 = (s3.config_id, s3.L, s3.q_min, s3.ER_min, s3.k_SL, s3.R_TP)
    observed_s4 = (s4.config_id, s4.W, s4.k_SL, s4.R_TP)
    if observed_s3 != S3_R2_ANCHOR.as_tuple():
        raise R3ManifestError("R2 S3-03 anchor projection drifted")
    if observed_s4 != S4_R2_ANCHOR.as_tuple():
        raise R3ManifestError("R2 S4-02 anchor projection drifted")


_BY_ID = {row.config_id: row for row in FROZEN_R3_ROSTER}


def get_r3_config(config_id: str) -> R3S3Config | R3S4Config:
    if type(config_id) is not str:
        raise R3ManifestError("config_id must be exact str")
    try:
        return _BY_ID[config_id]
    except KeyError as exc:
        raise R3ManifestError("config_id is outside the R3 roster") from exc


def assert_registered_r3_config(config: R3S3Config | R3S4Config) -> None:
    if type(config) not in (R3S3Config, R3S4Config):
        raise R3ManifestError("config must use an exact R3 config DTO")
    if _BY_ID.get(config.config_id) != config:
        raise R3ManifestError("config does not exactly match its frozen R3 row")


def _row_payload(row: R3S3Config | R3S4Config) -> dict[str, object]:
    return asdict(row)


def r3_manifest_contract_payload() -> dict[str, object]:
    """Return a fresh canonical payload containing every preregistered axis."""

    return {
        "schema_version": R3_MANIFEST_CONTRACT_VERSION,
        "preregistration_document_sha256": PREREGISTRATION_DOCUMENT_SHA256,
        "r2_research_document_sha256": R2_RESEARCH_DOCUMENT_SHA256,
        "contract_versions": {
            "S3": R3_S3_GATE_CONTRACT_VERSION,
            "S4": R3_S4_GATE_CONTRACT_VERSION,
        },
        "moving_axes": {
            "S3": ["S_min", "M_min_bp"],
            "S4": ["z_entry", "d_min_bp"],
        },
        "anchors": {
            "S3": asdict(S3_R2_ANCHOR),
            "S4": asdict(S4_R2_ANCHOR),
        },
        "roster": [_row_payload(row) for row in FROZEN_R3_ROSTER],
        "excluded_cells": {
            "S3": [list(cell) for cell in S3_EXCLUDED_CELLS],
            "S4": [list(cell) for cell in S4_EXCLUDED_CELLS],
        },
        "saturation_and_duplication": {
            "S4_z_entry_lt": S4_SATURATION_Z_LT,
            "S4_d_min_bp_lt": S4_DUPLICATE_D_MIN_BP_LT,
        },
        "relaxation_rays": [asdict(ray) for ray in R3_RELAXATION_RAYS],
        "adjacency_edges": [list(edge) for edge in R3_ADJACENCY_EDGES],
    }


R3_MANIFEST_CONTRACT_HASH = canonical_sha256(r3_manifest_contract_payload())


@dataclass(frozen=True, slots=True)
class R3StrategyContract:
    family: str
    key: str
    version: str
    preregistration_document_sha256: str
    manifest_contract_hash: str
    contract_hash: str

    def __post_init__(self) -> None:
        if self.family not in ("S3", "S4"):
            raise R3ManifestError("strategy contract family must be S3 or S4")
        _str(self.key, "strategy contract key")
        _str(self.version, "strategy contract version")
        _hex64(
            self.preregistration_document_sha256,
            "strategy preregistration document hash",
        )
        _hex64(self.manifest_contract_hash, "strategy manifest contract hash")
        _hex64(self.contract_hash, "strategy contract hash")


def _strategy_contract(family: str, version: str) -> R3StrategyContract:
    key = f"rob974.r3.{family.lower()}.threshold-relaxation"
    payload = {
        "family": family,
        "key": key,
        "version": version,
        "preregistration_document_sha256": PREREGISTRATION_DOCUMENT_SHA256,
        "manifest_contract_hash": R3_MANIFEST_CONTRACT_HASH,
        "roster": [
            _row_payload(row)
            for row in FROZEN_R3_ROSTER
            if row.config_id.startswith(f"{family}-")
        ],
    }
    return R3StrategyContract(
        family=family,
        key=key,
        version=version,
        preregistration_document_sha256=PREREGISTRATION_DOCUMENT_SHA256,
        manifest_contract_hash=R3_MANIFEST_CONTRACT_HASH,
        contract_hash=canonical_sha256(payload),
    )


R3_S3_STRATEGY_CONTRACT = _strategy_contract("S3", R3_S3_GATE_CONTRACT_VERSION)
R3_S4_STRATEGY_CONTRACT = _strategy_contract("S4", R3_S4_GATE_CONTRACT_VERSION)

validate_r3_manifest(FROZEN_R3_ROSTER)
validate_r3_relaxation_rays(R3_RELAXATION_RAYS)
validate_r2_anchor_projection()
