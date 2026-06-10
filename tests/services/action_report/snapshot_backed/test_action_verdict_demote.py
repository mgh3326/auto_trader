from app.services.action_report.snapshot_backed.action_verdict import (
    demote_for_quality,
)


def test_non_common_always_rejected_even_if_buy():
    assert demote_for_quality("buy_review", frozenset({"non_common_stock"})) == (
        "rejected",
        "non_common_stock",
    )


def test_unknown_common_is_data_gap_for_buy():
    assert demote_for_quality("buy_review", frozenset({"common_stock_unknown"})) == (
        "data_gap",
        "common_stock_unknown",
    )


def test_quality_demotes_buy_to_watch_in_priority_order():
    assert demote_for_quality("buy_review", frozenset({"penny", "illiquid"})) == (
        "watch_only",
        "penny",
    )
    assert demote_for_quality("buy_review", frozenset({"abnormal_spike"})) == (
        "watch_only",
        "abnormal_spike",
    )


def test_clean_buy_unchanged():
    assert demote_for_quality("buy_review", frozenset()) == ("buy_review", None)


def test_non_buy_not_upgraded():
    # 이미 honest 하향된 verdict은 품질로 끌어올리지 않는다(non_common 제외).
    assert demote_for_quality("watch_only", frozenset({"penny"})) == (
        "watch_only",
        None,
    )
    assert demote_for_quality("data_gap", frozenset()) == ("data_gap", None)
