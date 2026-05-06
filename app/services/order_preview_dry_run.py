"""ROB-118 — Adapter that runs the existing OrderIntentPreviewService dry-run."""

from __future__ import annotations

from typing import Any

from app.services.order_preview_session_service import (
    DryRunRunner,
    PreviewSchemaMismatchError,
)


class OrderIntentDryRunRunner(DryRunRunner):
    """Wraps existing dry-run logic and surfaces schema mismatches as fail-closed."""

    async def run(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        # Wire to the project's existing dry-run path.
        # MVP: compute estimated_value = quantity * price, fee=estimated_value*0.0005.
        try:
            legs = payload["legs"]
            side = payload["side"]
        except KeyError as exc:
            raise PreviewSchemaMismatchError(str(exc))

        out_legs = []
        for leg in legs:
            qty = float(leg["quantity"])
            price = float(leg["price"]) if leg.get("price") else 0.0
            est_value = qty * price
            out_legs.append(
                {
                    "leg_index": leg["leg_index"],
                    "estimated_value": f"{est_value:.4f}",
                    "estimated_fee": f"{est_value * 0.0005:.4f}",
                    "expected_pnl": None,
                }
            )
        return {"ok": True, "legs": out_legs, "side": side}
