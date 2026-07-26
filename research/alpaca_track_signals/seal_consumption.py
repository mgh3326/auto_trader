"""ROB-1061 H3 — the ONLY module in this package allowed to import
``alpaca_track_seal`` (H2). Every other H3 module reaches sealed values (the
16 configs, the $25/$10 order-size floors, gate thresholds, universe, ...)
through the accessors here — never by importing ``configs``/``params``/
``artifact`` directly, and NEVER by re-typing a sealed literal (H2-lock item
18: "H3~H6는 이 봉인 레코드를 읽기 전용으로만 소비한다. 하드코딩된 사본을
각 모듈이 재정의하면 테스트가 실패한다").

Fails closed at first use if the freshly-rebuilt H2 seal's semantic hash has
drifted from the pinned ``artifact.SEALED_ARTIFACT_SEMANTIC_HASH`` — the same
check ``alpaca_track_seal.registry_cli.build_registration_plan`` performs for
its own runtime entry point, applied here for H3's.
"""

from __future__ import annotations

from dataclasses import dataclass

import artifact as art
import configs as cfg

__all__ = [
    "SealDriftError",
    "UnknownConfigIdError",
    "load_sealed_configs_and_params",
    "min_broker_order_usd",
    "min_strategy_target_usd",
    "sealed_config_by_id",
]


class SealDriftError(RuntimeError):
    """The freshly-rebuilt H2 seal no longer matches the pinned semantic
    hash — refuse to consume a drifted seal (fail closed, mirrors
    ``registry_cli.SemanticHashDriftError``)."""


class UnknownConfigIdError(KeyError):
    """A requested ``config_id`` is not one of the sealed 16."""


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
