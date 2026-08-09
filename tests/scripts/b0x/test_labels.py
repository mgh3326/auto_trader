"""B0-X v1.4 account-history label scope and wording guards."""

from __future__ import annotations

from scripts.b0x import contract as contract_module
from scripts.b0x.kr.mock import LANE as KR_LANE
from scripts.b0x.labels import (
    B0X_IDENTITY_LABELS,
    SHARED_ACCOUNT_HISTORY,
    SHARED_HISTORY_ACCOUNTS,
    account_history_labels,
)


def test_shared_history_scope_starts_with_binance_sidecar_only() -> None:
    assert SHARED_HISTORY_ACCOUNTS == frozenset({"binance_spot_demo_sidecar"})
    assert account_history_labels("binance_spot_demo_sidecar") == (
        SHARED_ACCOUNT_HISTORY,
    )


def test_adding_an_account_key_is_the_only_scope_expansion() -> None:
    expanded_scope = SHARED_HISTORY_ACCOUNTS | {KR_LANE}

    assert account_history_labels(KR_LANE) == ()
    assert account_history_labels(KR_LANE, accounts=expanded_scope) == (
        SHARED_ACCOUNT_HISTORY,
    )


def test_kr_and_us_are_not_in_the_initial_scope() -> None:
    assert account_history_labels(KR_LANE) == ()
    assert account_history_labels("alpaca_paper_lab") == ()


def test_writer_singleton_matches_v1_4_section_three() -> None:
    writer_label = next(
        label for label in B0X_IDENTITY_LABELS if label.startswith("WRITER_SINGLETON")
    )

    assert "B0-X 측 주문 생성 주체는 B0-X 어댑터 하나다" in writer_label
    assert "disarm 운영 조치로 확보" in writer_label
    assert "오염 게이트의 fail-closed 관측" in writer_label
    assert "이 계좌의 주문 생성 주체는 B0-X 어댑터 하나뿐이다" not in writer_label


def test_shared_history_wording_is_complete_and_non_exaggerated() -> None:
    assert SHARED_ACCOUNT_HISTORY.startswith("SHARED_ACCOUNT_HISTORY —")
    assert "B0-X 이전에 다른 주체가 사용한 이력" in SHARED_ACCOUNT_HISTORY
    assert "BTC·SOL dust" in SHARED_ACCOUNT_HISTORY
    assert "2026-07-29 ROB-1150 사고 기록 4건" in SHARED_ACCOUNT_HISTORY
    assert "프로덕션 데모 스캘핑 봇과 자격증명을 공유" in SHARED_ACCOUNT_HISTORY
    assert "2026-08-09 disarm" in SHARED_ACCOUNT_HISTORY
    assert "체결·잔고 이력 전부를 B0-X 산출로 읽으면 안 된다" in SHARED_ACCOUNT_HISTORY
    assert "이제 단독" not in SHARED_ACCOUNT_HISTORY


def test_contract_stamp_points_to_v1_4_reference() -> None:
    assert contract_module.CONTRACT_VERSION == "v1.4"
    assert set(contract_module.CONTRACT_CLAUSES) == {"§8 v1.4 ②", "§8 v1.4 ③"}
    assert (
        contract_module.CONTRACT_FILE_SHA256_REFERENCE_ONLY
        == "bce7104bd1a3f36a253baecc05d8bc960ad1c41a82de4c345d6659320ad1f5f8"
    )
