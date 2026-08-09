"""US-lane contract stamp: v1.6 plus the clauses this adapter implements.

The shared B0-X package still carries older sidecar-oriented provenance.  The
US adapter must not restamp that as its binding contract, so its record replaces
the generic stamp with this lane-local v1.6 identity.  The whole-file digest is
recorded for reference only: the signed binding is version + quoted clauses.
"""

from __future__ import annotations

from typing import Any, Final

CONTRACT_PATH: Final[str] = "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md"
CONTRACT_VERSION: Final[str] = "v1.6"
# Recorded for traceability only.  The binding is version + cited clauses;
# this digest is deliberately not a substitute for checking those clauses.
CONTRACT_FILE_SHA256_REFERENCE: Final[str] = (
    "a3922894dcb91c2888daa2b33a9bfb9fab48a1c660ffc16deead09c530faea14"
)

CONTRACT_CLAUSES: Final[dict[str, str]] = {
    "§1": (
        "US 는 추가로 `CROSS_MARKET_TRANSFER_UNVALIDATED`(B0 는 KR 스코프 계량 — "
        "US 이식은 이중 미검증)."
    ),
    "§2-2 v1.1": (
        "표가 없거나 `STALE` 이거나 `MAX_TABLE_AGE` 초과면 그 사이클은 주문 0 … US 36h."
    ),
    "§4 US": (
        "종목당 신규 $150~450 · 종목당 총 신규×5 · 동시 포지션 ≤10 · 일 신규 ≤3 · "
        "일 손실 −2.5% NAV → kill · 세션 US RTH."
    ),
    "§8 v1.5 ①": (
        "envelope 상한 = 브로커 진실 파생 — 동시 포지션/자기 미체결/일일 신규를 "
        "브로커의 같은 사이클 응답에서 파생한다."
    ),
    "§8 v1.6": ("kis_mock 한정 원장 미체결 예외; 일반 원칙은 브로커 진실 유지."),
}

ACCOUNT_MAP_FACTS: Final[dict[str, str]] = {
    "canonical_surface": "operator_contract.yaml",
    "account_lane": "account_lanes.alpaca_paper_lab=B0-X-US",
    "writer": (
        "strategy_order_exceptions.b0x-adapter-orders-20260808."
        "writer=b0x_adapter_single"
    ),
    "surface": (
        "strategy_order_exceptions.b0x-adapter-orders-20260808.surfaces "
        "contains alpaca_paper_lab"
    ),
    "legacy_lab_cleanup_constraint": (
        "alpaca_account_cleanup_20260805 records alpaca_paper_lab suffix "
        "a9e6cd / UBER qty=1 as a one-shot legacy cleanup; its forbidden list "
        "includes reuse_after_execution and scope expansion. It is not B0-X "
        "ownership evidence and must not be reused."
    ),
}


def contract_stamp() -> dict[str, Any]:
    """Return the v1.6/version-plus-clauses binding recorded by US cycles."""

    return {
        "path": CONTRACT_PATH,
        "version": CONTRACT_VERSION,
        "clauses": dict(CONTRACT_CLAUSES),
        "file_sha256_reference_only": CONTRACT_FILE_SHA256_REFERENCE,
    }


def account_map_stamp() -> dict[str, Any]:
    """Record the machine-readable account-map facts, not a guessed narrative."""

    return dict(ACCOUNT_MAP_FACTS)


__all__ = [
    "ACCOUNT_MAP_FACTS",
    "CONTRACT_FILE_SHA256_REFERENCE",
    "CONTRACT_CLAUSES",
    "CONTRACT_PATH",
    "CONTRACT_VERSION",
    "account_map_stamp",
    "contract_stamp",
]
