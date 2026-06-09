from external_strategy_sieve.validation import runner


def test_build_plan_lists_five_candidates():
    plan = runner.build_plan(
        symbols=["BTCUSDT", "ETHUSDT"], from_month="2023-01", to_month="2024-12"
    )
    assert len(plan["candidates"]) == 5
    assert plan["fee_bps"] == 4.0
    assert plan["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert "params_hash" in plan


def test_signal_dispatch_resolves_all_five():
    for spec in runner._SIGNAL_FNS.values():
        assert callable(spec)


def test_frozen_intervals_are_fetcher_supported():
    import pit_klines_fetcher

    intervals = {
        spec["interval"]
        for spec in runner.build_plan(["BTCUSDT"], "2023-01", "2024-12")[
            "candidates"
        ].values()
    }
    assert intervals <= set(pit_klines_fetcher.SUPPORTED_INTERVALS)
