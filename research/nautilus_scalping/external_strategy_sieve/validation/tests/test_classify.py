from validated_gate import GateReport

from external_strategy_sieve.validation.classify import classify


def _report(verdict, gross_net, oos_net, oos_exp, oos_pf=1.5, all_pos=True):
    report = GateReport(verdict=verdict)
    report.results = {
        "gross": {"net_pnl": gross_net, "trades": 500},
        "net_after_cost": {
            "net_pnl": oos_net,
            "trades": 500,
            "expectancy": oos_exp,
        },
    }
    train = 10.0 if all_pos else -1.0
    report.per_fold = [
        {"fold": "train", "net_pnl": train, "expectancy": 1.0, "profit_factor": 1.2},
        {"fold": "val", "net_pnl": 10.0, "expectancy": 1.0, "profit_factor": 1.2},
        {
            "fold": "oos",
            "net_pnl": oos_net,
            "expectancy": oos_exp,
            "profit_factor": oos_pf,
        },
    ]
    return report


def test_insufficient_data_is_research():
    klass, _ = classify(_report("insufficient_data", 0, 0, 0))
    assert klass == "research_candidate"


def test_gross_negative_not_validated_is_reject():
    klass, _ = classify(
        _report("not_validated", gross_net=-50.0, oos_net=-10.0, oos_exp=-0.1)
    )
    assert klass == "reject"


def test_gross_positive_failed_gate_is_research():
    klass, _ = classify(
        _report("not_validated", gross_net=80.0, oos_net=5.0, oos_exp=0.05)
    )
    assert klass == "research_candidate"


def test_validated_below_floor_is_shadow():
    klass, _ = classify(
        _report("validated", gross_net=200, oos_net=30, oos_exp=0.02, all_pos=False),
        notional=1000.0,
    )
    assert klass == "shadow_candidate"


def test_validated_above_floor_all_folds_is_demo_ready():
    klass, _ = classify(
        _report("validated", gross_net=400, oos_net=120, oos_exp=0.2, all_pos=True),
        notional=1000.0,
    )
    assert klass == "demo_ready_candidate"
