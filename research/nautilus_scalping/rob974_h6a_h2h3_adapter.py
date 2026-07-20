"""ROB-981 (ROB-974 R2 H6-A) CP8 -- the sole production H2/H3 identity adapter.

Independently reconstructs and verifies the REAL merged H2 (ROB-979,
``H2_MERGE_SHA``) and H3 (ROB-980, ``H3_MERGE_SHA``) production surfaces
before ever constructing a ``provenance="production"``
``rob974_h6a_identity.CampaignConfigRow``/``StrategyContractProvenance``.
Never trusts H3's own frozen ``StrategyContract.contract_hash`` attribute
blindly -- recomputes ``hash_contract_payload(strategy_contract_payload(...))``
from scratch and compares. Delegates ALL H2 contract-drift detection to
``rob974_h3_h2_adapter.verify_h2_contract`` -- that module is already the
sole reviewed concrete H2 integration seam (ROB-980 CP8); this adapter must
never grow a second, divergent H2 field inventory.

H4 (walk-forward runner) and PBO source pins are explicitly OUT of ROB-981's
scope (H4 is a separate, still-open predecessor issue). ``build_production_
campaign_row_specs`` therefore still accepts ``shared_components``/
``pit_component_by_slug``/``frozen_config_component_by_slug``/
``policy_component_by_slug``/``cost_component_by_slug`` as CALLER-injected
maps -- exactly like ``rob974_h6a_identity.build_campaign_row_specs`` -- and
H4 is expected to supply the real values later. This module supplies ONLY
the H2/H3-sourced slice of identity (the verified row/contract data), never a
full real empirical campaign identity -- see
``PRODUCTION_FULL_CAMPAIGN_IDENTITY_STATUS``.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
real merged H2/H3 research modules and the H6-A identity kernel.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import rob974_h2_s3_engine as _h2_s3_engine  # noqa: F401 -- import-drift proof
import rob974_h2_s4_engine as _h2_s4_engine  # noqa: F401 -- import-drift proof
import rob974_h3_h2_adapter as h3_h2_adapter
import rob974_h3_manifest as h3_manifest
import rob974_h6a_identity as h6a

__all__ = [
    "H2_MERGE_SHA",
    "H3_MERGE_SHA",
    "PRODUCTION_FULL_CAMPAIGN_IDENTITY_STATUS",
    "RESEARCH_DOCUMENT_SHA256",
    "ContractDriftError",
    "build_production_campaign_row_specs",
    "build_production_contracts",
    "build_production_rows",
    "verify_h2h3_contract",
]

# The verified merge commits this adapter was reviewed against (ROB-981 R2
# orch relay: H2 merged 0b81057c, H3 merged 09aaa034). A production build
# does not compare against these directly -- they document provenance only;
# the actual gate is `verify_h2h3_contract`'s independent recomputation.
H2_MERGE_SHA = "0b81057c7b450f1539c836fd6cfa5732fb5800c5"
H3_MERGE_SHA = "09aaa034e4272a52863939b139f8e711ed1977aa"

# The pinned ROB-974 research authority SHA (also independently verified by
# H2's and H3's own worker packets) -- H3's RESEARCH_DOCUMENT_SHA256 must
# cite exactly this value or the manifest has drifted from its authority.
RESEARCH_DOCUMENT_SHA256 = (
    "2f535196cf0f0a03292e8f4c1806794ffbf8282ba7b5c3f564a930763577a009"
)

# H6-A CP8 supplies only the H2/H3-sourced (row/contract) slice of identity.
# H4's walk-forward runner and PBO implementation source pins are a separate,
# still-open predecessor (ROB-974 R2 H4) -- a full real empirical
# full-campaign identity cannot be claimed until H4 supplies its real exact
# source pins. This module exposes a fail-closed builder H4 will later call;
# it never fabricates a substitute for H4's own pins.
PRODUCTION_FULL_CAMPAIGN_IDENTITY_STATUS = "DEFERRED_UNTIL_H4_SOURCE_PINS"


class ContractDriftError(RuntimeError):
    """The real merged H2/H3 production surface no longer matches this
    adapter's independently reconstructed expectation -- refuse rather than
    silently guess at a substitute identity."""


def _contract_drift(message: str) -> ContractDriftError:
    return ContractDriftError(f"CONTRACT_DRIFT: {message}")


def verify_h2h3_contract() -> None:
    """Fail closed if the real merged H2 or H3 production surface drifted.

    Called at the start of every builder below -- nothing here is cached
    across calls, so a drift introduced after import is still caught before
    the next production row/contract is built.
    """
    try:
        h3_h2_adapter.verify_h2_contract()
    except h3_h2_adapter.ContractDriftError as exc:
        raise _contract_drift(f"H2 (via H3's reviewed integration seam): {exc}") from exc

    if h3_manifest.RESEARCH_DOCUMENT_SHA256 != RESEARCH_DOCUMENT_SHA256:
        raise _contract_drift(
            "H3 RESEARCH_DOCUMENT_SHA256 no longer matches the pinned ROB-974 "
            "research authority SHA"
        )

    try:
        h3_manifest.validate_manifest(h3_manifest.FROZEN_H3_ROSTER)
        h3_manifest.validate_contract_seals(
            h3_manifest.S3_STRATEGY_CONTRACT, h3_manifest.S4_STRATEGY_CONTRACT
        )
    except (TypeError, ValueError) as exc:
        raise _contract_drift(f"H3 manifest/contract seal check failed: {exc}") from exc

    # Independently recompute both contract hashes from scratch -- never
    # trust the frozen StrategyContract's own `.contract_hash` attribute.
    for slug, contract in (
        ("S3", h3_manifest.S3_STRATEGY_CONTRACT),
        ("S4", h3_manifest.S4_STRATEGY_CONTRACT),
    ):
        recomputed = h3_manifest.hash_contract_payload(
            h3_manifest.strategy_contract_payload(slug)
        )
        if recomputed != contract.contract_hash:
            raise _contract_drift(
                f"{slug}: recomputed contract hash does not match H3's declared "
                "contract_hash -- stale or tampered manifest"
            )


_S3_PARAM_FIELDS: tuple[str, ...] = ("L", "q_min", "ER_min", "k_SL", "R_TP", "design_type")
_S4_PARAM_FIELDS: tuple[str, ...] = (
    "W",
    "z_entry",
    "d_min_bp",
    "k_SL",
    "R_TP",
    "design_type",
)


def _row_from_config(config: object, *, fields: tuple[str, ...]) -> h6a.CampaignConfigRow:
    return h6a.CampaignConfigRow(
        row_id=config.config_id,
        params={name: getattr(config, name) for name in fields},
        hypothesis=config.hypothesis_utf8.decode("utf-8"),
        authority_label=config.authority_label,
        provenance="production",
    )


def build_production_rows() -> list[h6a.CampaignConfigRow]:
    """The real 48 (config_id, params, hypothesis, authority_label) rows,
    sourced from H3's frozen manifest -- verified before use."""
    verify_h2h3_contract()
    rows = [
        _row_from_config(config, fields=_S3_PARAM_FIELDS)
        for config in h3_manifest.FROZEN_S3_CONFIGS
    ]
    rows += [
        _row_from_config(config, fields=_S4_PARAM_FIELDS)
        for config in h3_manifest.FROZEN_S4_CONFIGS
    ]
    return rows


def build_production_contracts() -> dict[str, h6a.StrategyContractProvenance]:
    """The real S3/S4 structured strategy contracts, sourced from H3's frozen
    seals -- ``contract_hash`` is the INDEPENDENTLY recomputed value (never
    H3's own attribute), pinned against H3's declared value via
    ``expected_contract_hash`` (stale-pin protection, same mechanism as the
    fixture path)."""
    verify_h2h3_contract()
    contracts: dict[str, h6a.StrategyContractProvenance] = {}
    for slug, contract in (
        ("S3", h3_manifest.S3_STRATEGY_CONTRACT),
        ("S4", h3_manifest.S4_STRATEGY_CONTRACT),
    ):
        recomputed_hash = h3_manifest.hash_contract_payload(
            h3_manifest.strategy_contract_payload(slug)
        )
        contracts[slug] = h6a.StrategyContractProvenance(
            strategy_slug=slug,
            strategy_key=contract.key,
            strategy_version=contract.version,
            contract_hash=recomputed_hash,
            contract_key=contract.key,
            provenance="production",
            expected_contract_hash=contract.contract_hash,
        )
    return contracts


def build_production_campaign_row_specs(
    *,
    shared_components: Mapping[str, Any],
    pit_component_by_slug: Mapping[str, Any],
    frozen_config_component_by_slug: Mapping[str, Any],
    policy_component_by_slug: Mapping[str, Any],
    cost_component_by_slug: Mapping[str, Any],
) -> tuple[h6a.H6ARowSpec, ...]:
    """Build all 48 production-tagged row identity specs from the REAL
    verified H2/H3 predecessors plus caller-injected remaining components
    (H4/PBO source pins -- not yet available, supplied by H4 later).

    This is NOT a real empirical full-campaign identity --
    ``PRODUCTION_FULL_CAMPAIGN_IDENTITY_STATUS`` stays
    ``DEFERRED_UNTIL_H4_SOURCE_PINS`` until an H4 caller supplies its own
    real, non-fixture, non-synthetic components here.
    """
    verify_h2h3_contract()
    rows = build_production_rows()
    contracts = build_production_contracts()
    return h6a.build_campaign_row_specs(
        rows,
        contracts=contracts,
        shared_components=shared_components,
        pit_component_by_slug=pit_component_by_slug,
        frozen_config_component_by_slug=frozen_config_component_by_slug,
        policy_component_by_slug=policy_component_by_slug,
        cost_component_by_slug=cost_component_by_slug,
    )
