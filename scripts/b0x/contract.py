"""B0-X contract identity stamped onto every observation artifact.

One module so that a contract amendment is a one-file edit, and so a
verifier has a single place to compare the running code against the
signed document.

Why the version string binds and the file digest does not
--------------------------------------------------------
An earlier revision identified the contract by its **whole-file sha256**.
That reads like the strongest possible check and is in fact the weakest
useful one: the contract is a living document, so every amendment — even
one that does not touch this lane — changes the digest. A stamp that
changes on every unrelated edit cannot distinguish "the code is running
against the wrong contract" from "the contract gained a paragraph
elsewhere", so the digest alone produced false drift reports (X-U
verification) while proving nothing about the clauses this lane actually
obeys.

So the binding identity here is :data:`CONTRACT_VERSION` plus the verbatim
clauses in :data:`CONTRACT_CLAUSES` — the text this code implements. The
digest is retained as :data:`CONTRACT_FILE_SHA256_REFERENCE_ONLY`, named
so it cannot be mistaken for the criterion: it is provenance for the
reader, not a gate.

Amending the contract therefore means editing the version and the quoted
clause here, in the same commit as whatever behaviour changed — which is
the coupling the digest was failing to provide.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Final

CONTRACT_PATH: Final[str] = "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md"

#: Binding identity. Bump together with the clauses below.
CONTRACT_VERSION: Final[str] = "v1.7"

#: Provenance only — NOT a drift criterion. See the module docstring.
CONTRACT_FILE_SHA256_REFERENCE_ONLY: Final[str] = (
    "0d09e1ce4d175da75de17958880491965ea6cae8d13764853f23fbc0348f596a"
)

#: Verbatim §8 v1.7 amendment this package records.
CONTRACT_CLAUSES: Final[dict[str, str]] = {
    "§8 v1.7": (
        "「스케줄러 등록 없음」 개정 — **스케줄러 (Prefect)는 시각만 소유한다**: "
        "표 빌드 실행(KR 07:45·US 22:00)과 orch 기상 nudge(사이클 슬롯)에 한정. "
        "전략 판단·주문 파생·dispatch·워커 실행은 불변(orch/워커 소유, "
        "harvest-before-dispatch 유지). 근거 = 수동 원샷 장전 누락 4회 실측. "
        "실행 표면·envelope·승격 절차 무변경."
    ),
}

#: Account map — the machine-readable surface is canonical (v1.3 ①).
ACCOUNT_MAP_REPO: Final[str] = "auto_trader-operator"
ACCOUNT_MAP_COMMIT_UNAVAILABLE: Final[str] = "UNAVAILABLE"
ACCOUNT_MAP_CANONICAL_SURFACE: Final[str] = "operator_contract.yaml"
ACCOUNT_MAP_REFERENCE_SURFACE: Final[str] = "mock/CLAUDE.md"
_GIT_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

#: Sidecar lane standing, narrowed by v1.3 ②. Stamped on sidecar artifacts so
#: a reader cannot mistake a buy-side fill-fidelity sample for a round-trip
#: strategy result: the sell side of B0-X is observed on the Upbit shadow lane,
#: not here.
SIDECAR_SCOPE: Final[str] = "buy_side_fill_fidelity_sample_only"


def contract_stamp() -> dict[str, Any]:
    """Contract identity block for an observation record."""

    return {
        "path": CONTRACT_PATH,
        "version": CONTRACT_VERSION,
        "clauses": dict(CONTRACT_CLAUSES),
        "file_sha256_reference_only": CONTRACT_FILE_SHA256_REFERENCE_ONLY,
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


def _unavailable_account_map_stamp(
    *,
    source_path: Path | None,
    reason: str,
    repository_path: Path | None = None,
) -> dict[str, Any]:
    """Return an explicit unknown rather than inventing a historic commit."""

    return {
        "repo": ACCOUNT_MAP_REPO,
        "commit": ACCOUNT_MAP_COMMIT_UNAVAILABLE,
        "commit_status": "unavailable",
        "commit_reason": reason,
        "source_path": None if source_path is None else str(source_path),
        "repository_path": (None if repository_path is None else str(repository_path)),
        "branch": ACCOUNT_MAP_COMMIT_UNAVAILABLE,
        "branch_reason": reason,
        "reachable_from_origin_main": None,
        "canonical_surface": ACCOUNT_MAP_CANONICAL_SURFACE,
        "reference_surface": ACCOUNT_MAP_REFERENCE_SURFACE,
    }


def account_map_stamp(*, account_map_path: Path | None) -> dict[str, Any]:
    """Stamp the account-map worktree that supplied this cycle's table path.

    `account_map_path` is injected by the cycle from its actual
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

    return {
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


__all__ = [
    "ACCOUNT_MAP_CANONICAL_SURFACE",
    "ACCOUNT_MAP_COMMIT_UNAVAILABLE",
    "ACCOUNT_MAP_REFERENCE_SURFACE",
    "ACCOUNT_MAP_REPO",
    "CONTRACT_CLAUSES",
    "CONTRACT_FILE_SHA256_REFERENCE_ONLY",
    "CONTRACT_PATH",
    "CONTRACT_VERSION",
    "SIDECAR_SCOPE",
    "account_map_stamp",
    "contract_stamp",
]
