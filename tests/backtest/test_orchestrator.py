"""Tests for the backtest autoresearch orchestrator."""

import sys
from pathlib import Path

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import orchestrator


def _write_results(path: Path, rows: list[str]) -> None:
    path.write_text(
        "experiment\tcv_score\tmean\tstd\tmin_fold\ttest_score\tstatus\tdescription\n"
        + "\n".join(rows)
        + "\n"
    )


def test_get_best_cv_score_reads_only_keep_rows(tmp_path: Path) -> None:
    results_path = tmp_path / "results.tsv"
    _write_results(
        results_path,
        [
            "exp1\t4.200000\t4.2\t0.1\t3.9\tNA\tkeep\tfirst",
            "exp2\t4.500000\t4.5\t0.2\t4.0\tNA\trevert\tsecond",
            "exp3\t4.400000\t4.4\t0.1\t4.1\tNA\tkept\tthird",
        ],
    )

    assert orchestrator.get_best_cv_score(results_path) == 4.4


def test_read_last_result_row_returns_latest_entry(tmp_path: Path) -> None:
    results_path = tmp_path / "results.tsv"
    _write_results(
        results_path,
        [
            "exp10\t4.100000\t4.1\t0.1\t3.9\tNA\tkeep\talpha",
            "exp11\t4.300000\t4.3\t0.2\t4.0\tNA\trevert\tbeta",
        ],
    )

    result = orchestrator.read_last_result_row(results_path)

    assert result is not None
    assert result.experiment == "exp11"
    assert result.cv_score == 4.3
    assert result.status == "revert"
    assert result.description == "beta"


def test_read_best_result_row_returns_top_kept_entry(tmp_path: Path) -> None:
    results_path = tmp_path / "results.tsv"
    _write_results(
        results_path,
        [
            "exp10\t4.100000\t4.1\t0.1\t3.9\tNA\tkeep\talpha",
            "exp11\t4.600000\t4.6\t0.2\t4.0\tNA\trevert\tbeta",
            "exp12\t4.500000\t4.5\t0.2\t4.1\tNA\tkept\tgamma",
        ],
    )

    result = orchestrator.read_best_result_row(results_path)

    assert result is not None
    assert result.experiment == "exp12"
    assert result.cv_score == 4.5
    assert result.status == "kept"
    assert result.description == "gamma"


def test_get_recent_experiments_returns_latest_rows_newest_first(
    tmp_path: Path,
) -> None:
    results_path = tmp_path / "results.tsv"
    _write_results(
        results_path,
        [
            "exp10\t4.100000\t4.1\t0.1\t3.9\tNA\tkeep\talpha",
            "exp11\t4.300000\t4.3\t0.2\t4.0\tNA\trevert\tbeta",
            "exp12\t4.500000\t4.5\t0.2\t4.1\tNA\tkept\tgamma",
        ],
    )

    assert orchestrator.get_recent_experiments(results_path, n=2) == [
        ("exp12", "kept", "gamma"),
        ("exp11", "revert", "beta"),
    ]


def test_resolve_description_prefers_cli_value() -> None:
    assert (
        orchestrator.resolve_description(
            cli_description="manual override",
            commit_subject="exp207: from git",
        )
        == "manual override"
    )


def test_format_duration_renders_human_readable_output() -> None:
    assert orchestrator.format_duration(47) == "47s"
    assert orchestrator.format_duration(83) == "1m 23s"
    assert orchestrator.format_duration(3723) == "1h 2m 3s"


def test_apply_status_treats_auto_no_commit_as_skip() -> None:
    stats = orchestrator.Stats(initial_best_score=4.228733, current_best_score=4.228733)

    orchestrator.apply_status(stats, status="skip")

    assert stats.skipped == 1
    assert stats.reverted == 0
    assert stats.crashed == 0
    assert stats.consecutive_reverts == 0


def test_revert_limit_triggers_only_after_exceeding_threshold() -> None:
    stats = orchestrator.Stats(initial_best_score=4.228733, current_best_score=4.228733)

    orchestrator.apply_status(stats, status="revert")
    orchestrator.apply_status(stats, status="revert")
    orchestrator.apply_status(stats, status="revert")

    assert orchestrator.revert_limit_exceeded(stats, max_consecutive_reverts=3) is False

    orchestrator.apply_status(stats, status="revert")

    assert orchestrator.revert_limit_exceeded(stats, max_consecutive_reverts=3) is True


def test_build_ai_prompt_includes_recent_experiment_history(
    tmp_path: Path, monkeypatch
) -> None:
    results_path = tmp_path / "results.tsv"
    _write_results(
        results_path,
        [
            "exp24\t4.210000\t4.2\t0.2\t4.0\tNA\tkeep\tadd BTC trend gate",
            "exp25\t4.180000\t4.2\t0.2\t4.0\tNA\trevert\thalve AVAX size",
            "exp26\t4.170000\t4.2\t0.2\t4.0\tNA\trevert\traise stop-loss to 2.5%",
        ],
    )
    monkeypatch.setattr(orchestrator, "RESULTS_TSV", results_path)

    prompt = orchestrator.build_ai_prompt(
        round_number=3, total_rounds=20, best_score=4.228733
    )

    assert "Recent experiments (DO NOT repeat these):" in prompt
    assert "exp26 [REVERT]" in prompt
    assert "raise stop-loss to 2.5%" in prompt
    assert "exp25 [REVERT]" in prompt
    assert "exp24 [KEEP]" in prompt
