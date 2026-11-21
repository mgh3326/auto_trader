"""hk 5852 T9a — the two shadow rule ids the Upbit KRW experiment's sleeves use.

``CANARY_RULES`` is a closed list (``store.create_sleeve`` refuses any other id),
so these tests pin the two ids, their market, and that the operator's CLI accepts
them. They do not create a sleeve: that is an operator action (runbook).
"""

from __future__ import annotations

import pytest

from app.services.strategy_sleeves.vocabulary import CANARY_RULES
from scripts import strategy_sleeves as cli

pytestmark = pytest.mark.unit

TREND = "at5852.upbit_krw_trend_shadow"
PULLBACK = "at5852.upbit_krw_pullback_shadow"


@pytest.mark.parametrize("rule_id", [TREND, PULLBACK])
def test_each_shadow_rule_id_is_a_canary_rule_keyed_by_its_own_id(rule_id):
    rule = CANARY_RULES[rule_id]
    assert rule.rule_id == rule_id


@pytest.mark.parametrize("rule_id", [TREND, PULLBACK])
def test_each_shadow_rule_runs_only_in_the_crypto_market(rule_id):
    assert CANARY_RULES[rule_id].markets == frozenset({"crypto"})


@pytest.mark.parametrize(
    ("rule_id", "section"),
    [
        (TREND, "docs/design/5852-upbit-krw-shadow-strategy.md §4.1"),
        (PULLBACK, "docs/design/5852-upbit-krw-shadow-strategy.md §4.2"),
    ],
)
def test_each_shadow_rule_names_its_design_section(rule_id, section):
    assert CANARY_RULES[rule_id].design_section == section


@pytest.mark.parametrize("rule_id", [TREND, PULLBACK])
def test_metric_and_horizon_are_reference_text_not_binding_numbers(rule_id):
    rule = CANARY_RULES[rule_id]
    assert rule.metric.startswith("reference only")
    assert rule.horizon.startswith("reference only")


def test_the_two_families_are_distinct_epochs_from_each_other():
    assert TREND != PULLBACK
    assert CANARY_RULES[TREND].title != CANARY_RULES[PULLBACK].title


def test_the_existing_canary_ids_are_untouched():
    """Adding ids must not rename or re-scope the ROB-1458 / hk 3795 rules."""
    assert {
        "rob1458.us_near_market_entry",
        "rob1458.us_add_at_minus8",
        "rob1458.kr_cut_minus20",
        "rob1458.parking_idle_cash",
        "rob1458.crypto_sell_expiry",
        "rob1458.crypto_b3",
        "at3795.basket1_value_split_entry",
    } <= set(CANARY_RULES)
    assert CANARY_RULES["rob1458.crypto_b3"].markets == frozenset({"crypto"})


@pytest.mark.parametrize("rule_id", [TREND, PULLBACK])
def test_the_create_command_accepts_each_shadow_rule_id(rule_id):
    args = cli._parser().parse_args(
        [
            "--database-url-env",
            "TRADING_DB_URL",
            "create",
            "--sleeve-key",
            "probe-key",
            "--rule-id",
            rule_id,
            "--account-mode",
            "upbit",
            "--market",
            "crypto",
            "--state",
            "shadow",
            "--budget-cap",
            "1950000",
            "--loss-limit",
            "1000000",
            "--reason",
            "probe",
            "--actor",
            "operator:probe",
        ]
    )
    assert args.rule_id == rule_id
