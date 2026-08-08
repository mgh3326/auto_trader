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

from typing import Any, Final

CONTRACT_PATH: Final[str] = "~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md"

#: Binding identity. Bump together with the clauses below.
CONTRACT_VERSION: Final[str] = "v1.4"

#: Provenance only — NOT a drift criterion. See the module docstring.
CONTRACT_FILE_SHA256_REFERENCE_ONLY: Final[str] = (
    "bce7104bd1a3f36a253baecc05d8bc960ad1c41a82de4c345d6659320ad1f5f8"
)

#: Verbatim §8 v1.4 clauses this package implements.
CONTRACT_CLAUSES: Final[dict[str, str]] = {
    "§8 v1.4 ②": (
        "관측 산출물에 **`SHARED_ACCOUNT_HISTORY` 라벨** 부착(**과거 dust·사고 "
        "이력 계좌**)."
    ),
    "§8 v1.4 ③": (
        "writer=1 문언 정합: 「B0-X 측 단일 writer + 계좌 배타성은 운영 "
        "조치(disarm)로 확보, 방어는 오염 게이트의 fail-closed 관측」."
    ),
}

#: Account map — the machine-readable surface is canonical (v1.3 ①).
ACCOUNT_MAP_REPO: Final[str] = "auto_trader-operator"
ACCOUNT_MAP_COMMIT: Final[str] = "3f402919fca5b68bda187e8e521fc886aefb022a"
ACCOUNT_MAP_CANONICAL_SURFACE: Final[str] = "operator_contract.yaml"
ACCOUNT_MAP_REFERENCE_SURFACE: Final[str] = "mock/CLAUDE.md"

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


def account_map_stamp() -> dict[str, Any]:
    """Account-map identity block — canonical surface named explicitly."""

    return {
        "repo": ACCOUNT_MAP_REPO,
        "commit": ACCOUNT_MAP_COMMIT,
        "canonical_surface": ACCOUNT_MAP_CANONICAL_SURFACE,
        "reference_surface": ACCOUNT_MAP_REFERENCE_SURFACE,
    }


__all__ = [
    "ACCOUNT_MAP_CANONICAL_SURFACE",
    "ACCOUNT_MAP_COMMIT",
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
