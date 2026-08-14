"""Contract/account-map stamp — the identity every artifact carries.

Contract v1.7 (job brief): the binding identity is the **version string plus
quoted clause**, and the whole-file digest is demoted to a reference field.
These tests pin that shape, because the failure it prevents is silent: an
artifact that looks stamped while naming a contract the code no longer
implements.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.b0x import contract as contract_module
from scripts.b0x.cycle import render_cycle_report

V17_SCHEDULER_CLAUSE = (
    "「스케줄러 등록 없음」 개정 — **스케줄러 (Prefect)는 시각만 소유한다**: "
    "표 빌드 실행(KR 07:45·US 22:00)과 orch 기상 nudge(사이클 슬롯)에 한정. "
    "전략 판단·주문 파생·dispatch·워커 실행은 불변(orch/워커 소유, "
    "harvest-before-dispatch 유지). 근거 = 수동 원샷 장전 누락 4회 실측. "
    "실행 표면·envelope·승격 절차 무변경."
)
V17_CONTRACT_FILE_SHA256 = (
    "0d09e1ce4d175da75de17958880491965ea6cae8d13764853f23fbc0348f596a"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_operator_table_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Make an isolated operator worktree with a local origin/main reference."""

    repository_path = tmp_path / "operator.tbl"
    repository_path.mkdir()
    _git(repository_path, "init", "-q")
    _git(repository_path, "config", "user.email", "test@example.invalid")
    _git(repository_path, "config", "user.name", "B0-X test")
    (repository_path / "operator_contract.yaml").write_text("contract: test\n")
    _git(repository_path, "add", "operator_contract.yaml")
    _git(repository_path, "commit", "-q", "-m", "operator contract")
    _git(repository_path, "branch", "-M", "main")
    _git(repository_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    table_dir = repository_path / "policy-tables"
    table_dir.mkdir()
    return repository_path, table_dir


@pytest.mark.unit
def test_version_is_the_binding_identity() -> None:
    assert contract_module.CONTRACT_VERSION == "v1.7"
    stamp = contract_module.contract_stamp()
    assert stamp["version"] == "v1.7"


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
    assert clauses == {"§8 v1.7": V17_SCHEDULER_CLAUSE}


@pytest.mark.unit
def test_contract_file_digest_is_the_current_reference_value() -> None:
    stamp = contract_module.contract_stamp()
    assert contract_module.CONTRACT_FILE_SHA256_REFERENCE_ONLY == (
        V17_CONTRACT_FILE_SHA256
    )
    assert stamp["file_sha256_reference_only"] == V17_CONTRACT_FILE_SHA256


@pytest.mark.unit
def test_sidecar_scope_is_the_narrowed_v13_standing() -> None:
    assert contract_module.SIDECAR_SCOPE == "buy_side_fill_fidelity_sample_only"


@pytest.mark.unit
def test_account_map_stamp_uses_the_injected_table_worktree(
    tmp_path: Path,
) -> None:
    """The stamped commit comes from the table directory the cycle supplied."""

    repository_path, table_dir = _make_operator_table_repo(tmp_path)
    expected_head = _git(repository_path, "rev-parse", "HEAD")

    stamp = contract_module.account_map_stamp(account_map_path=table_dir)

    assert stamp["canonical_surface"] == "operator_contract.yaml"
    assert stamp["reference_surface"] == "mock/CLAUDE.md"
    assert stamp["repo"] == "auto_trader-operator"
    assert stamp["source_path"] == str(table_dir)
    assert stamp["repository_path"] == str(repository_path)
    assert stamp["commit"] == expected_head
    assert stamp["branch"] == "main"
    assert stamp["reachable_from_origin_main"] is True
    assert stamp["commit_status"] == "available"
    assert stamp["commit_reason"] is None


@pytest.mark.unit
def test_detached_orphan_account_map_head_is_marked_unpublished(
    tmp_path: Path,
) -> None:
    """A local-only detached commit is visible, not silently presented as main."""

    repository_path, table_dir = _make_operator_table_repo(tmp_path)
    _git(repository_path, "checkout", "-q", "--detach", "HEAD")
    (repository_path / "orphan.txt").write_text("not on origin/main\n")
    _git(repository_path, "add", "orphan.txt")
    _git(repository_path, "commit", "-q", "-m", "detached orphan")
    orphan_head = _git(repository_path, "rev-parse", "HEAD")

    stamp = contract_module.account_map_stamp(account_map_path=table_dir)

    assert stamp["commit"] == orphan_head
    assert stamp["branch"] == "detached"
    assert stamp["reachable_from_origin_main"] is False
    assert stamp["commit_status"] == "unpublished"
    assert stamp["commit_reason"] == ("account_map_head_not_reachable_from_origin_main")


@pytest.mark.unit
def test_non_git_account_map_source_is_honestly_stamped_unavailable(
    tmp_path: Path,
) -> None:
    """An invalid table path cannot silently revive a historic hard-coded SHA."""

    source_path = tmp_path / "not-an-operator-worktree"
    source_path.mkdir()
    stamp = contract_module.account_map_stamp(account_map_path=source_path)

    assert stamp["commit"] == "UNAVAILABLE"
    assert stamp["commit_status"] == "unavailable"
    assert stamp["commit_reason"] == "account_map_source_not_git_repository"
    assert stamp["source_path"] == str(source_path)
    assert stamp["reachable_from_origin_main"] is None
    assert "3f402919fca5b68bda187e8e521fc886aefb022a" not in repr(stamp)


@pytest.mark.unit
def test_unavailable_account_map_reason_is_visible_in_the_artifact_header(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "not-an-operator-worktree"
    source_path.mkdir()
    stamp = contract_module.account_map_stamp(account_map_path=source_path)
    report = render_cycle_report(
        {
            "lane": "binance_spot_demo_sidecar_buy_side_only",
            "at": "2026-08-13T00:00:00+00:00",
            "contract": contract_module.contract_stamp(),
            "account_map": stamp,
        },
        labels=(),
    )

    assert "auto_trader-operator@UNAVAILABLE" in report
    assert "reason=`account_map_source_not_git_repository`" in report
    assert f"source=`{source_path}`" in report
    assert "branch=`UNAVAILABLE`" in report
    assert "origin/main-reachable=`unknown`" in report
