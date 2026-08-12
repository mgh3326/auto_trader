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
ACCOUNT_MAP_REPO_PATH: Final[Path] = Path.home() / "services" / ACCOUNT_MAP_REPO
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


def _resolve_account_map_head() -> tuple[str, str | None]:
    """Read the exact operator-repo HEAD used for this account-map stamp.

    The operator repository is a read-only dependency here.  A missing or
    unreadable checkout is observation degradation, not evidence for a
    historical commit, so callers receive an explicit unavailable marker
    instead of a stale fallback.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ACCOUNT_MAP_REPO_PATH,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return ACCOUNT_MAP_COMMIT_UNAVAILABLE, "account_map_head_unavailable"
    except subprocess.SubprocessError:
        return ACCOUNT_MAP_COMMIT_UNAVAILABLE, "account_map_head_query_failed"

    if result.returncode != 0:
        return ACCOUNT_MAP_COMMIT_UNAVAILABLE, "account_map_head_query_failed"

    commit = result.stdout.strip()
    if not _GIT_COMMIT_RE.fullmatch(commit):
        return ACCOUNT_MAP_COMMIT_UNAVAILABLE, "account_map_head_invalid"
    return commit, None


def account_map_stamp() -> dict[str, Any]:
    """Account-map identity block — canonical surface and actual HEAD."""

    commit, reason = _resolve_account_map_head()

    return {
        "repo": ACCOUNT_MAP_REPO,
        "commit": commit,
        "commit_status": "available" if reason is None else "unavailable",
        "commit_reason": reason,
        "canonical_surface": ACCOUNT_MAP_CANONICAL_SURFACE,
        "reference_surface": ACCOUNT_MAP_REFERENCE_SURFACE,
    }


__all__ = [
    "ACCOUNT_MAP_CANONICAL_SURFACE",
    "ACCOUNT_MAP_COMMIT_UNAVAILABLE",
    "ACCOUNT_MAP_REFERENCE_SURFACE",
    "ACCOUNT_MAP_REPO",
    "ACCOUNT_MAP_REPO_PATH",
    "CONTRACT_CLAUSES",
    "CONTRACT_FILE_SHA256_REFERENCE_ONLY",
    "CONTRACT_PATH",
    "CONTRACT_VERSION",
    "SIDECAR_SCOPE",
    "account_map_stamp",
    "contract_stamp",
]
