"""Contract/account-map stamp — the identity every artifact carries.

Contract v1.3 ④ (job brief): the binding identity is the **version string
plus quoted clause**, and the whole-file digest is demoted to a reference
field. These tests pin that shape, because the failure it prevents is silent:
an artifact that looks stamped while naming a contract the code no longer
implements.
"""

from __future__ import annotations

import pytest

from scripts.b0x import contract as contract_module


@pytest.mark.unit
def test_version_is_the_binding_identity() -> None:
    assert contract_module.CONTRACT_VERSION == "v1.3"
    stamp = contract_module.contract_stamp()
    assert stamp["version"] == "v1.3"


@pytest.mark.unit
def test_digest_is_named_reference_only_and_is_not_the_criterion() -> None:
    """The field name itself is the guard against re-promoting the digest."""

    stamp = contract_module.contract_stamp()
    assert "file_sha256_reference_only" in stamp
    # No bare "sha"/"file_sha256" key that a reader could mistake for a gate.
    assert "sha" not in stamp
    assert "file_sha256" not in stamp
    assert "contract_sha" not in stamp


@pytest.mark.unit
def test_clauses_are_quoted_verbatim_not_merely_referenced() -> None:
    """A clause number with no text cannot be checked against the document."""

    clauses = contract_module.contract_stamp()["clauses"]
    assert set(clauses) == {"§8 v1.3 ①", "§8 v1.3 ②"}
    for key, text in clauses.items():
        assert len(text) > 40, f"{key} is a reference, not a quotation"
    # ① is the re-registration decision this job executes.
    assert "취소" in clauses["§8 v1.3 ①"]
    assert "재실행" in clauses["§8 v1.3 ①"]
    # ② is the narrowed sidecar standing.
    assert "매수측" in clauses["§8 v1.3 ②"]


@pytest.mark.unit
def test_sidecar_scope_is_the_narrowed_v13_standing() -> None:
    assert contract_module.SIDECAR_SCOPE == "buy_side_fill_fidelity_sample_only"


@pytest.mark.unit
def test_account_map_names_yaml_as_canonical_and_md_as_reference() -> None:
    """v1.3 ①: the machine-readable surface wins; prose is a reference."""

    stamp = contract_module.account_map_stamp()
    assert stamp["canonical_surface"] == "operator_contract.yaml"
    assert stamp["reference_surface"] == "mock/CLAUDE.md"
    assert stamp["repo"] == "auto_trader-operator"
    # The commit that actually carries the B0-X entries (PR #33).
    assert stamp["commit"].startswith("3f40291")


@pytest.mark.unit
def test_stale_pre_registration_account_map_sha_is_gone() -> None:
    """The old stamp pointed at a commit where B0-X was prose-only.

    That commit is exactly the state contract v1.3 ① calls an editing
    mistake, so no artifact may still claim it.
    """

    rendered = repr(contract_module.account_map_stamp()) + repr(
        contract_module.contract_stamp()
    )
    assert "7f95897" not in rendered
