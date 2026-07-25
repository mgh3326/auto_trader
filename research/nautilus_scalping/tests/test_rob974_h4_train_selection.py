from dataclasses import dataclass

import pytest
from rob974_h4_selection import (
    TrainCandidateTrace,
    TrainUnitMetric,
    run_train_global_configs,
    select_train_config,
)


def _trace(
    config_id: str, *, units: tuple[TrainUnitMetric, ...], pf: float
) -> TrainCandidateTrace:
    return TrainCandidateTrace(config_id, units, pf, 999.0, "1" * 64, "2" * 64)


def test_train_selection_uses_equal_weight_not_pooled_expectancy() -> None:
    traces = tuple(
        _trace(
            f"S3-{index:02d}",
            units=(
                TrainUnitMetric("XRPUSDT", 5, 1.0 if index == 0 else 2.0),
                TrainUnitMetric("DOGEUSDT", 5, 1.0 if index == 0 else 2.0),
                TrainUnitMetric("SOLUSDT", 4, -99.0),
            ),
            pf=1.0,
        )
        for index in range(24)
    )
    # All candidates tie on equal-weight E17 except config ID; their pooled
    # input is deliberately a non-authoritative 999bp.
    selection = select_train_config("S3", traces)
    assert selection.selected_config_id == "S3-01"


def test_selection_excludes_four_trade_units_and_uses_pf_then_id_ties() -> None:
    traces = []
    for index in range(24):
        units = (
            TrainUnitMetric("XRPUSDT", 4, 99.0),
            TrainUnitMetric("DOGEUSDT", 5, 1.0),
        )
        pf = 1.0
        if index in (4, 5):
            units = (
                TrainUnitMetric("XRPUSDT", 5, 3.0),
                TrainUnitMetric("DOGEUSDT", 5, 3.0),
            )
            pf = 1.2 if index == 4 else 1.2
        traces.append(_trace(f"S3-{index:02d}", units=units, pf=pf))
    selection = select_train_config("S3", tuple(traces))
    assert selection.selected_config_id == "S3-04"


@dataclass(frozen=True)
class _Config:
    config_id: str


def test_train_calls_each_global_config_once_with_fresh_engine() -> None:
    configs = tuple(_Config(f"S3-{index:02d}") for index in range(24))
    generated: list[str] = []
    engine_markers: list[object] = []

    def generator(config: _Config) -> str:
        generated.append(config.config_id)
        return config.config_id

    def factory():
        marker = object()
        engine_markers.append(marker)
        return lambda received: (marker, received)

    results = run_train_global_configs(
        strategy="S3",
        configs=configs,
        generator=generator,
        fresh_primary_engine=factory,
    )
    assert generated == [config.config_id for config in configs]
    assert len({id(marker) for marker in engine_markers}) == 24
    assert len(results) == 24
    with pytest.raises(ValueError, match="exact ordered"):
        run_train_global_configs(
            strategy="S3",
            configs=configs[:-1],
            generator=generator,
            fresh_primary_engine=factory,
        )
