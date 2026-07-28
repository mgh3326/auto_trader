"""Sealed P0-2 cost binding, transcribed from the amendment canonical.

Source of truth (do not paraphrase, do not re-derive):

    ~/work/herdr-inbox/krb1c-amendment-canonical-2026-07-28.json
    sha256 d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389

    .cost_binding        -> broker/product/channel, cost_basis, commissions
    .sell_tax_components -> per-market sell tax decomposition

Both markets carry ``cost_basis = REAL_TRADING_TARIFF`` and the Kiwoom real
tariff of 0.015% per side (``rate_e12 = 150_000_000``). The mock 0.35% rate is
display/reconciliation only (§2.5, §8.4) and is deliberately absent from this
module — it must never reach a numeric input.

``load_from_canonical`` re-reads the canonical JSON and asserts the transcribed
constants against it, so a drift in the sealed file surfaces as a failure
rather than as a silently stale copy.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence

from .reducer import (
    CostRecord,
    MarketCostInput,
    ReducerFailClosed,
    SellTaxComponent,
    reduce_records,
)

AMENDMENT_CANONICAL_SHA256 = (
    "d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389"
)
PARENT_CANONICAL_SHA256 = (
    "d5e1246b2072ad227d924e059091e27fa49719e42a5bfba651ae8f2bced9d6f1"
)
OFFICIAL_TARIFF_SNAPSHOT_SHA256 = (
    "8fd195fe7426ab66afca2ff131a08153afd114a2c0912b32e0924ba2434095af"
)

MARKETS: tuple[str, ...] = ("KOSPI", "KOSDAQ")

# .cost_binding
BUY_COMMISSION_RATE_E12 = 150_000_000
SELL_COMMISSION_RATE_E12 = 150_000_000
BROKER_ID = "kiwoom"
ACCOUNT_PRODUCT_ID = "KIWOOM_DOMESTIC_CASH_STOCK"
ORDER_CHANNEL_ID = "KIWOOM_OPENAPI_KRX"
COST_BASIS = "REAL_TRADING_TARIFF"

# .sell_tax_components
SELL_TAX_COMPONENTS: dict[str, tuple[tuple[str, int], ...]] = {
    "KOSPI": (
        ("KOSPI_SECURITIES_TRANSACTION_TAX", 500_000_000),
        ("KOSPI_RURAL_SPECIAL_TAX", 1_500_000_000),
    ),
    "KOSDAQ": (("KOSDAQ_SECURITIES_TRANSACTION_TAX", 2_000_000_000),),
}


def _record(market: str) -> CostRecord:
    return CostRecord(
        market=market,
        buy_commission_rate_e12=BUY_COMMISSION_RATE_E12,
        sell_commission_rate_e12=SELL_COMMISSION_RATE_E12,
        sell_tax_components=tuple(
            SellTaxComponent(code, rate) for code, rate in SELL_TAX_COMPONENTS[market]
        ),
        cost_basis=COST_BASIS,
        effective_from=None,
        effective_to=None,  # §2.5 — 현행표만 null
        source_snapshot_sha256=OFFICIAL_TARIFF_SNAPSHOT_SHA256,
        probe_reconciliation_status="PASS",
        mock_cost_relation="DIFFERENT",
        broker_id=BROKER_ID,
        account_product_id=ACCOUNT_PRODUCT_ID,
        order_channel_id=ORDER_CHANNEL_ID,
    )


def sealed_records() -> dict[str, tuple[CostRecord, ...]]:
    """The binding market_cost_records, one current-tariff record per market."""
    return {market: (_record(market),) for market in MARKETS}


def sealed_cost_inputs() -> dict[str, MarketCostInput]:
    """§3 reduction of the sealed records into B_m / S_m / A_m per market."""
    return {
        market: reduce_records(market, records)
        for market, records in sealed_records().items()
    }


# --------------------------------------------------------------------------
# canonical re-verification
# --------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_canonical_path() -> str:
    return os.path.expanduser(
        "~/work/herdr-inbox/krb1c-amendment-canonical-2026-07-28.json"
    )


def load_from_canonical(path: str | None = None) -> dict[str, MarketCostInput]:
    """Re-read the sealed canonical and cross-check the transcribed constants.

    Fails closed (§8.1(c)/(g)) on a hash mismatch or any numeric divergence.
    """
    path = path or default_canonical_path()
    actual = sha256_file(path)
    if actual != AMENDMENT_CANONICAL_SHA256:
        raise ReducerFailClosed(
            "8.1(c)",
            f"amendment canonical sha256 mismatch: expected "
            f"{AMENDMENT_CANONICAL_SHA256}, got {actual}",
        )

    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)

    binding = doc["cost_binding"]
    if binding["cost_basis"] != COST_BASIS:
        raise ReducerFailClosed(
            "2.5", f"canonical cost_basis={binding['cost_basis']!r}"
        )
    for key, expected in (
        ("buy_commission_rate_e12", BUY_COMMISSION_RATE_E12),
        ("sell_commission_rate_e12", SELL_COMMISSION_RATE_E12),
    ):
        if binding[key] != expected:
            raise ReducerFailClosed(
                "8.1(g)",
                f"canonical {key}={binding[key]} != transcribed {expected}",
            )
    for key, expected_str in (
        ("broker_id", BROKER_ID),
        ("account_product_id", ACCOUNT_PRODUCT_ID),
        ("order_channel_id", ORDER_CHANNEL_ID),
        ("source_snapshot_sha256", OFFICIAL_TARIFF_SNAPSHOT_SHA256),
    ):
        if binding[key] != expected_str:
            raise ReducerFailClosed(
                "8.1(d)",
                f"canonical {key}={binding[key]!r} != transcribed {expected_str!r}",
            )

    taxes = doc["sell_tax_components"]
    for market in MARKETS:
        got: Sequence[dict] = taxes[market]
        expected_pairs = SELL_TAX_COMPONENTS[market]
        if len(got) != len(expected_pairs):
            raise ReducerFailClosed(
                "8.1(g)",
                f"{market}: canonical has {len(got)} tax components, "
                f"transcribed {len(expected_pairs)}",
            )
        got_map = {c["component_code"]: c["rate_e12"] for c in got}
        for code, rate in expected_pairs:
            if got_map.get(code) != rate:
                raise ReducerFailClosed(
                    "8.1(g)",
                    f"{market}: canonical {code}={got_map.get(code)!r} != "
                    f"transcribed {rate}",
                )

    parent = doc["parent"]
    if parent["sha256"] != PARENT_CANONICAL_SHA256:
        raise ReducerFailClosed(
            "8.1(c)",
            f"parent canonical sha256 mismatch: expected "
            f"{PARENT_CANONICAL_SHA256}, canonical says {parent['sha256']}",
        )
    if parent.get("relationship") != "CHILD_AMENDMENT_APPEND_ONLY":
        raise ReducerFailClosed(
            "7.1",
            f"amendment must be an append-only child, got "
            f"{parent.get('relationship')!r}",
        )

    return sealed_cost_inputs()
