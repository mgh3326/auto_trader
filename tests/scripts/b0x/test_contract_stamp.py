"""Contract/account-map stamp — the identity every artifact carries.

Contract v1.7 (job brief): the binding identity is the **version string plus
quoted clause**, and the whole-file digest is demoted to a reference field.
These tests pin that shape, because the failure it prevents is silent: an
artifact that looks stamped while naming a contract the code no longer
implements.
"""

from __future__ import annotations

import subprocess
from typing import Any

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
def test_account_map_stamp_is_the_exact_head_read_by_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stamp cannot name a different commit than the account-map gate used."""

    gate_head = "a" * 40
    monkeypatch.setattr(
        contract_module, "_resolve_account_map_head", lambda: (gate_head, None)
    )

    stamp = contract_module.account_map_stamp()
    assert stamp["canonical_surface"] == "operator_contract.yaml"
    assert stamp["reference_surface"] == "mock/CLAUDE.md"
    assert stamp["repo"] == "auto_trader-operator"
    assert stamp["commit"] == gate_head
    assert stamp["commit_status"] == "available"
    assert stamp["commit_reason"] is None


@pytest.mark.unit
def test_account_map_head_lookup_queries_the_operator_repo_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provenance source is exactly ``git rev-parse HEAD`` in that repo."""

    expected_head = "b" * 40
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=f"{expected_head}\n")

    monkeypatch.setattr(contract_module.subprocess, "run", fake_run)

    commit, reason = contract_module._resolve_account_map_head()

    assert commit == expected_head
    assert reason is None
    assert captured["command"] == ["git", "rev-parse", "HEAD"]
    assert captured["cwd"] == contract_module.ACCOUNT_MAP_REPO_PATH


@pytest.mark.unit
def test_failed_account_map_lookup_is_honestly_stamped_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed lookup cannot silently revive a historic hard-coded commit."""

    def fail_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("operator checkout unavailable")

    monkeypatch.setattr(contract_module.subprocess, "run", fail_run)

    stamp = contract_module.account_map_stamp()

    assert stamp["commit"] == "UNAVAILABLE"
    assert stamp["commit_status"] == "unavailable"
    assert stamp["commit_reason"] == "account_map_head_unavailable"
    assert "3f402919fca5b68bda187e8e521fc886aefb022a" not in repr(stamp)


@pytest.mark.unit
def test_unavailable_account_map_reason_is_visible_in_the_artifact_header() -> None:
    report = render_cycle_report(
        {
            "lane": "binance_spot_demo_sidecar_buy_side_only",
            "at": "2026-08-13T00:00:00+00:00",
            "contract": contract_module.contract_stamp(),
            "account_map": {
                "repo": "auto_trader-operator",
                "commit": "UNAVAILABLE",
                "canonical_surface": "operator_contract.yaml",
                "commit_reason": "account_map_head_unavailable",
            },
        },
        labels=(),
    )

    assert "auto_trader-operator@UNAVAILABLE" in report
    assert "reason=`account_map_head_unavailable`" in report
