import pytest
from rob974_h4_runner import non_selected_oos_paths, run_selected_oos_paths


def test_selected_winner_runs_one_generator_and_three_fresh_paths() -> None:
    calls: list[str] = []

    def generator(winner: str) -> str:
        calls.append(f"generator:{winner}")
        return "unique-input"

    def engine(scenario: str):
        calls.append(f"factory:{scenario}")
        return lambda accepted: (scenario, accepted)

    paths = run_selected_oos_paths(
        winner="S3-00", generator=generator, fresh_engine=engine
    )
    assert paths == (
        ("base13", "unique-input"),
        ("primary_stress17", "unique-input"),
        ("upward_stress22", "unique-input"),
    )
    assert calls == [
        "generator:S3-00",
        "factory:base13",
        "factory:primary_stress17",
        "factory:upward_stress22",
    ]


def test_oos_rejects_shared_engine_and_has_explicit_non_selected_paths() -> None:
    def shared(accepted: str) -> str:
        return accepted

    with pytest.raises(ValueError, match="fresh engine"):
        run_selected_oos_paths(
            winner="S3-00",
            generator=lambda winner: winner,
            fresh_engine=lambda scenario: shared,
        )
    assert non_selected_oos_paths() == ("not_selected", "not_selected", "not_selected")
