"""ROB-1061 H3 — the ONLY module in this package allowed to import
``alpaca_track_seal`` (H2). Every other H3 module reaches sealed values (the
16 configs, the $25/$10 order-size floors, gate thresholds, universe, ...)
through the accessors here — never by importing ``configs``/``params``/
``artifact``/``identity`` directly, and NEVER by re-typing a sealed literal
(H2-lock item 18: "H3~H6는 이 봉인 레코드를 읽기 전용으로만 소비한다.
하드코딩된 사본을 각 모듈이 재정의하면 테스트가 실패한다"). Engine modules
that need the ``ConfigSpec`` TYPE for a signature annotation import it from
HERE (``sc.ConfigSpec``), never ``import configs`` directly — that import
would itself violate the "only this module touches alpaca_track_seal" rule
this docstring states.

Fails closed at first use if the freshly-rebuilt H2 seal's semantic hash has
drifted from the pinned ``artifact.SEALED_ARTIFACT_SEMANTIC_HASH`` — the same
check ``alpaca_track_seal.registry_cli.build_registration_plan`` performs for
its own runtime entry point, applied here for H3's.

ROB-1061 adversarial-verification remediation (2026-07-26): two findings
fixed here.

1. (AC18 re-declaration) ``sizing.py`` used to hardcode ``INITIAL_EQUITY_USD
   = 2000.0`` and ``AP_A1_BASE_SLOT_DIVISOR = 32``, on the claim that H2's
   seal "never captures" the $2,000 initial-equity / $62.50 base-slot values.
   That claim was false: ``identity.build_components_for_config``'s
   ``frozen_config`` component embeds both literals, and
   ``artifact.SealedArtifact.to_dict()`` folds every config's
   ``identity_components`` into ``SEALED_ARTIFACT_SEMANTIC_HASH`` — so those
   values ARE inside the seal already, H3 just wasn't reading them from it.
   ``initial_equity_usd``/``ap_a1_base_slot_usd`` below read them from the
   seal's own ``frozen_config`` identity component (never re-typed), and a
   simulated re-seal (equity/base-slot literals changed in ``identity.py``)
   now moves ``sizing.py``'s numbers too, instead of silently diverging.
2. (NO_THRESHOLD_RELAXATION enforcement) neither engine previously verified
   that the ``ConfigSpec`` it was handed is actually one of the sealed 16 —
   only ``config.family`` was checked. A forged config reusing a real
   ``config_id`` with a relaxed ``threshold`` (or any other param) passed
   straight through. ``assert_sealed_config`` below closes that gap: an
   engine calls it once, at the top of every decision, and it fails closed
   unless the config's ``canonical_hash``/``params`` are byte-identical to
   the sealed row of the same ``config_id``.
"""

from __future__ import annotations

from dataclasses import dataclass

import artifact as art
import configs as cfg
import identity as ident

__all__ = [
    "ConfigNotSealedError",
    "ConfigSpec",
    "SealDriftError",
    "UnknownConfigIdError",
    "ap_a1_base_slot_usd",
    "assert_sealed_config",
    "initial_equity_usd",
    "load_sealed_configs_and_params",
    "min_broker_order_usd",
    "min_strategy_target_usd",
    "sealed_config_by_id",
]

# The ONLY sanctioned way for an engine module to reference the ``ConfigSpec``
# type without importing ``configs`` (part of H2, ``alpaca_track_seal``)
# itself — see module docstring.
ConfigSpec = cfg.ConfigSpec


class SealDriftError(RuntimeError):
    """The freshly-rebuilt H2 seal no longer matches the pinned semantic
    hash — refuse to consume a drifted seal (fail closed, mirrors
    ``registry_cli.SemanticHashDriftError``)."""


class UnknownConfigIdError(KeyError):
    """A requested ``config_id`` is not one of the sealed 16."""


class ConfigNotSealedError(ValueError):
    """A ``ConfigSpec`` handed to an engine boundary is NOT byte-identical to
    the sealed row of the same ``config_id`` — either an unknown id, or a
    forged/relaxed config parading under a real one (e.g. a lowered
    ``threshold``). ``config.family`` alone can never detect this (family is
    shared by all 8 configs in a strategy); this is the actual
    ``NO_THRESHOLD_RELAXATION`` (``run_status.no_threshold_relaxation``)
    enforcement point."""


@dataclass(frozen=True)
class SealedConfigsAndParams:
    configs: tuple[cfg.ConfigSpec, ...]
    params: object  # alpaca_track_seal.params.SealedParams (kept as `object`
    # here to avoid a second cross-module type-name binding — callers that
    # need typed access already import `params` themselves for annotations)


def load_sealed_configs_and_params() -> SealedConfigsAndParams:
    """Rebuild the H2 seal fresh (pure, deterministic, no network) and fail
    closed if it has drifted from the pinned semantic hash."""
    sealed = art.build_sealed_artifact()
    actual = sealed.semantic_hash()
    if actual != art.SEALED_ARTIFACT_SEMANTIC_HASH:
        raise SealDriftError(
            f"H2 seal semantic hash {actual!r} does not match the pinned "
            f"{art.SEALED_ARTIFACT_SEMANTIC_HASH!r} — refusing to consume a "
            "drifted seal"
        )
    return SealedConfigsAndParams(configs=sealed.configs, params=sealed.params)


def sealed_config_by_id(config_id: str) -> cfg.ConfigSpec:
    bundle = load_sealed_configs_and_params()
    for config in bundle.configs:
        if config.config_id == config_id:
            return config
    raise UnknownConfigIdError(f"{config_id!r} is not one of the sealed 16 configs")


def min_strategy_target_usd() -> float:
    """The sealed $25 strategy-level order-size floor
    (``params.RunStatusBlock.min_strategy_target_usd``) — SS11.5/SS12.5's
    ``MIN_TARGET_NOTIONAL``. Never hardcode ``25`` in a sizing module; call
    this instead."""
    bundle = load_sealed_configs_and_params()
    return float(bundle.params.run_status.min_strategy_target_usd)


def min_broker_order_usd() -> float:
    """The sealed $10 broker-level order-size floor
    (``params.RunStatusBlock.min_broker_order_usd``) — used only to assert
    the strategy floor is strictly higher (AC11), never as a strategy
    sizing input itself."""
    bundle = load_sealed_configs_and_params()
    return float(bundle.params.run_status.min_broker_order_usd)


def _sealed_frozen_config_component(family: str) -> dict:
    """The ``frozen_config`` ROB-846 identity component for the first sealed
    config of ``family`` — this is where H2's seal actually carries the
    ``initial_equity_usd``/``base_slot_usd`` (AP-A1 only) literals (see
    module docstring), NOT ``params.RunStatusBlock``."""
    bundle = load_sealed_configs_and_params()
    for config in bundle.configs:
        if config.family == family:
            return ident.build_components_for_config(config, bundle.params)[
                "frozen_config"
            ]
    raise ValueError(f"no sealed config with family={family!r}")


def initial_equity_usd() -> float:
    """The sealed fixed initial equity (SS11.5/SS12.5: "초기 equity $2,000
    고정"), read from H2's ``frozen_config`` identity component — the SAME
    value for AP-A1 and AP-A2 (both components are checked equal here; a
    mismatch means the two families' seals have drifted apart and this fails
    closed rather than silently picking one). Never a re-typed literal."""
    a1 = float(_sealed_frozen_config_component("AP-A1")["initial_equity_usd"])
    a2 = float(_sealed_frozen_config_component("AP-A2")["initial_equity_usd"])
    if a1 != a2:
        raise SealDriftError(
            f"initial_equity_usd diverges between AP-A1 ({a1!r}) and AP-A2 "
            f"({a2!r}) sealed frozen_config components — refusing to pick one"
        )
    return a1


def ap_a1_base_slot_usd() -> float:
    """The sealed AP-A1 base slot (SS11.5: "base_slot = equity/32 = $62.50"),
    read from H2's ``frozen_config`` identity component (AP-A1-family only —
    AP-A2 has no fixed base_slot, its base_slot is ``equity/k``, k being a
    swept param). Never a re-typed literal."""
    return float(_sealed_frozen_config_component("AP-A1")["base_slot_usd"])


def assert_sealed_config(config: cfg.ConfigSpec) -> None:
    """Fail closed unless ``config`` is byte-identical (``canonical_hash`` +
    ``params``) to the sealed row of the same ``config_id`` — the actual
    ``NO_THRESHOLD_RELAXATION`` enforcement point (see module docstring).
    Call this once, at the top of every engine decision, in addition to (not
    instead of) the ``config.family`` check — family alone cannot detect a
    forged/relaxed config reusing a real ``config_id``."""
    try:
        sealed = sealed_config_by_id(config.config_id)
    except UnknownConfigIdError as exc:
        raise ConfigNotSealedError(
            f"{config.config_id!r} is not one of the sealed 16 configs"
        ) from exc
    if config.canonical_hash != sealed.canonical_hash or dict(config.params) != dict(
        sealed.params
    ):
        raise ConfigNotSealedError(
            f"{config.config_id!r} does not match its sealed definition — "
            "this looks like a forged or relaxed config (e.g. a lowered "
            "threshold) reusing a real config_id"
        )
