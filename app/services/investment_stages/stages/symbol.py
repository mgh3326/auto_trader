"""Deterministic per-symbol stage (ROB-369 E12).

Per-symbol ``symbol`` snapshots are captured by ``SymbolSnapshotCollector`` but
no stage consumed them, so the captured per-symbol context never reached Hermes
(orphaned in *every* market — crypto observed it, but kr/us were affected too).

This stage surfaces the captured snapshots into ``stage_inputs``: one
``key_point`` per resolved symbol (held marker + name/sector/market_cap, plus
quote liquidity when the collector enriched it), and requested-but-unresolved
symbols under ``missing_data``. It is descriptive context, not a directional
call, so the verdict is always ``NEUTRAL`` — no bull/bear is invented from
symbol metadata. Read-only over persisted snapshots; no LLM, no broker calls.

Known limitation (ROB-369 / ROB-414): ``SymbolSnapshotCollector`` resolves
KR metadata from ``stock_info`` and US metadata from ``stock_info`` with a
``us_symbol_universe`` fallback for unheld candidates (ROB-414); quotes are
enriched only for KR + ``kis_live``. Crypto reads ``upbit_symbol_universe``
for metadata but has no quote adapter yet, so crypto symbols resolve thin —
this stage reports genuinely unresolvable tickers honestly under
``missing_data`` (``unresolved_symbols`` with per-ticker reason codes for US)
rather than fabricating metadata.
"""

from __future__ import annotations

from typing import Any

from app.models.investment_snapshots import InvestmentSnapshot
from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages._symbols import normalize_symbol
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)


def _held_symbols(context: StageContext) -> set[str]:
    """Normalized union of held + reference holdings (awareness only).

    Unions ALL portfolio snapshots in the bundle (a bundle normally carries
    one, but being defensive is cheap here).
    """
    held: set[str] = set()
    for snap in context.snapshots_for("portfolio"):
        payload = snap.payload_json or {}
        for key in ("holdings", "reference_holdings"):
            rows = payload.get(key) or []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get("ticker"), str):
                        held.add(normalize_symbol(row["ticker"]))
    return held


class SymbolStage:
    stage_type = "symbol"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snaps = context.snapshots_for("symbol")
        resolved: list[tuple[InvestmentSnapshot, dict[str, Any]]] = []
        missing: list[str] = []
        unresolved_reasons: dict[str, str] = {}
        for snap in snaps:
            payload = snap.payload_json or {}
            if payload.get("symbol"):
                resolved.append((snap, payload))
                continue
            unresolved = payload.get("unresolved")
            if isinstance(unresolved, list):
                for item in unresolved:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("symbol"), str)
                        and isinstance(item.get("reason_code"), str)
                    ):
                        unresolved_reasons[item["symbol"]] = item["reason_code"]
            if isinstance(payload.get("missing_symbols"), list):
                missing.extend(
                    s for s in payload["missing_symbols"] if isinstance(s, str)
                )

        # Nothing usable to synthesize (e.g. the "no symbols supplied"
        # unavailable snapshot carries neither key) → let the runner mark this
        # stage UNAVAILABLE rather than emit an empty NEUTRAL artifact.
        if not resolved and not missing:
            raise UnavailableStageError("symbol snapshots missing")

        held = _held_symbols(context)

        def _is_held(sym: str) -> bool:
            return normalize_symbol(sym) in held

        key_points: list[str] = []
        cited: list[StageCitation] = []
        for snap, payload in resolved:
            sym = str(payload.get("symbol", "?"))
            tag = "보유" if _is_held(sym) else "관심"
            bits: list[str] = []
            if payload.get("name"):
                bits.append(str(payload["name"]))
            if payload.get("sector"):
                bits.append(str(payload["sector"]))
            if payload.get("market_cap") is not None:
                bits.append(f"시총={payload['market_cap']}")
            quote = payload.get("quote")
            if isinstance(quote, dict) and quote.get("status") == "ok":
                if quote.get("spread_bps") is not None:
                    bits.append(f"스프레드={quote['spread_bps']}bps")
            detail = ", ".join(bits) if bits else "메타데이터 없음"
            key_points.append(f"[{tag}] {sym}: {detail}")
            cited.append(
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="symbol",
                    payload_path="$",
                )
            )

        held_resolved = [
            str(payload.get("symbol", "?"))
            for _snap, payload in resolved
            if _is_held(str(payload.get("symbol", "")))
        ]
        if resolved:
            summary = f"심볼 {len(resolved)}건"
            if held_resolved:
                summary += f" · 보유: {', '.join(held_resolved)}"
        else:
            summary = "해결된 심볼 없음"

        missing_data: list[str] = []
        if missing:
            uniq = sorted(set(missing))
            rendered = [
                f"{s} ({unresolved_reasons[s]})" if s in unresolved_reasons else s
                for s in uniq
            ]
            missing_data.append(f"unresolved_symbols: {', '.join(rendered)}")

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.NEUTRAL,
            confidence=60 if resolved else 20,
            summary=summary,
            key_points=key_points,
            missing_data=missing_data,
            cited_snapshots=cited,
        )
