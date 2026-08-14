"""US-lane contract stamp: v1.7 plus the clauses this adapter implements.

The shared B0-X package still carries older sidecar-oriented provenance.  The
US adapter must not restamp that as its binding contract, so its record replaces
the generic stamp with this lane-local v1.7 identity.  The whole-file digest is
recorded for reference only: the signed binding is version + quoted clauses.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Final

CONTRACT_PATH: Final[str] = "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md"
CONTRACT_VERSION: Final[str] = "v1.7"
# Recorded for traceability only.  The binding is version + cited clauses;
# this digest is deliberately not a substitute for checking those clauses.
CONTRACT_FILE_SHA256_REFERENCE: Final[str] = (
    "0d09e1ce4d175da75de17958880491965ea6cae8d13764853f23fbc0348f596a"
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
    "§8 v1.7": (
        "「스케줄러 등록 없음」 개정 — **스케줄러 (Prefect)는 시각만 소유한다**: "
        "표 빌드 실행(KR 07:45·US 22:00)과 orch 기상 nudge(사이클 슬롯)에 한정. "
        "전략 판단·주문 파생·dispatch·워커 실행은 불변(orch/워커 소유, "
        "harvest-before-dispatch 유지). 근거 = 수동 원샷 장전 누락 4회 실측. "
        "실행 표면·envelope·승격 절차 무변경."
    ),
}

ACCOUNT_MAP_REPO: Final[str] = "auto_trader-operator"
ACCOUNT_MAP_COMMIT_UNAVAILABLE: Final[str] = "UNAVAILABLE"
ACCOUNT_MAP_CANONICAL_SURFACE: Final[str] = "operator_contract.yaml"
ACCOUNT_MAP_REFERENCE_SURFACE: Final[str] = "mock/CLAUDE.md"
_GIT_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

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
    """Return the v1.7/version-plus-clauses binding recorded by US cycles."""

    return {
        "path": CONTRACT_PATH,
        "version": CONTRACT_VERSION,
        "clauses": dict(CONTRACT_CLAUSES),
        "file_sha256_reference_only": CONTRACT_FILE_SHA256_REFERENCE,
    }


def _run_git(
    command: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str] | None:
    """Run a bounded, read-only Git query without raising into a cycle."""

    try:
        return subprocess.run(
            ["git", *command],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return None
    except subprocess.SubprocessError:
        return None


def _with_us_account_map_facts(stamp: dict[str, Any]) -> dict[str, Any]:
    """Add US-lane facts without replacing the worktree provenance binding."""

    return {**stamp, **ACCOUNT_MAP_FACTS}


def _unavailable_account_map_stamp(
    *,
    source_path: Path | None,
    reason: str,
    repository_path: Path | None = None,
) -> dict[str, Any]:
    """Return an explicit unknown rather than inventing a historic commit."""

    return _with_us_account_map_facts(
        {
            "repo": ACCOUNT_MAP_REPO,
            "commit": ACCOUNT_MAP_COMMIT_UNAVAILABLE,
            "commit_status": "unavailable",
            "commit_reason": reason,
            "source_path": None if source_path is None else str(source_path),
            "repository_path": (
                None if repository_path is None else str(repository_path)
            ),
            "branch": ACCOUNT_MAP_COMMIT_UNAVAILABLE,
            "branch_reason": reason,
            "reachable_from_origin_main": None,
            "canonical_surface": ACCOUNT_MAP_CANONICAL_SURFACE,
            "reference_surface": ACCOUNT_MAP_REFERENCE_SURFACE,
        }
    )


def account_map_stamp(*, account_map_path: Path | None) -> dict[str, Any]:
    """Stamp the account-map worktree that supplied this cycle's table path.

    ``account_map_path`` is injected by the cycle from its actual
    ``--table-dir`` value. The resolver never consults a separate shared
    checkout: using one would let a later branch switch rewrite provenance for
    a table the cycle did not consume.
    """

    if account_map_path is None:
        return _unavailable_account_map_stamp(
            source_path=None, reason="account_map_source_not_supplied"
        )

    source_path = Path(account_map_path).expanduser()
    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd=source_path)
    if root_result is None:
        return _unavailable_account_map_stamp(
            source_path=source_path, reason="account_map_source_query_unavailable"
        )
    if root_result.returncode != 0:
        return _unavailable_account_map_stamp(
            source_path=source_path, reason="account_map_source_not_git_repository"
        )

    root_text = root_result.stdout.strip()
    if not root_text:
        return _unavailable_account_map_stamp(
            source_path=source_path, reason="account_map_repository_path_invalid"
        )
    repository_path = Path(root_text)
    if not (repository_path / ACCOUNT_MAP_CANONICAL_SURFACE).is_file():
        return _unavailable_account_map_stamp(
            source_path=source_path,
            repository_path=repository_path,
            reason="account_map_canonical_surface_missing",
        )

    head_result = _run_git(["rev-parse", "HEAD"], cwd=repository_path)
    if head_result is None or head_result.returncode != 0:
        return _unavailable_account_map_stamp(
            source_path=source_path,
            repository_path=repository_path,
            reason="account_map_head_query_failed",
        )
    commit = head_result.stdout.strip()
    if not _GIT_COMMIT_RE.fullmatch(commit):
        return _unavailable_account_map_stamp(
            source_path=source_path,
            repository_path=repository_path,
            reason="account_map_head_invalid",
        )

    branch_result = _run_git(["branch", "--show-current"], cwd=repository_path)
    if branch_result is None or branch_result.returncode != 0:
        branch = ACCOUNT_MAP_COMMIT_UNAVAILABLE
        branch_reason: str | None = "account_map_branch_query_failed"
    else:
        branch = branch_result.stdout.strip() or "detached"
        branch_reason = None

    reachability_result = _run_git(
        ["merge-base", "--is-ancestor", commit, "origin/main"],
        cwd=repository_path,
    )
    if reachability_result is not None and reachability_result.returncode == 0:
        reachable_from_origin_main: bool | None = True
        commit_status = "available"
        commit_reason: str | None = None
    elif reachability_result is not None and reachability_result.returncode == 1:
        reachable_from_origin_main = False
        commit_status = "unpublished"
        commit_reason = "account_map_head_not_reachable_from_origin_main"
    else:
        reachable_from_origin_main = None
        commit_status = "reachability_unavailable"
        commit_reason = "account_map_origin_main_query_failed"

    return _with_us_account_map_facts(
        {
            "repo": ACCOUNT_MAP_REPO,
            "commit": commit,
            "commit_status": commit_status,
            "commit_reason": commit_reason,
            "source_path": str(source_path),
            "repository_path": str(repository_path),
            "branch": branch,
            "branch_reason": branch_reason,
            "reachable_from_origin_main": reachable_from_origin_main,
            "canonical_surface": ACCOUNT_MAP_CANONICAL_SURFACE,
            "reference_surface": ACCOUNT_MAP_REFERENCE_SURFACE,
        }
    )


__all__ = [
    "ACCOUNT_MAP_CANONICAL_SURFACE",
    "ACCOUNT_MAP_COMMIT_UNAVAILABLE",
    "ACCOUNT_MAP_FACTS",
    "ACCOUNT_MAP_REFERENCE_SURFACE",
    "ACCOUNT_MAP_REPO",
    "CONTRACT_FILE_SHA256_REFERENCE",
    "CONTRACT_CLAUSES",
    "CONTRACT_PATH",
    "CONTRACT_VERSION",
    "account_map_stamp",
    "contract_stamp",
]
