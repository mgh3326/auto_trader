"""ROB-1294 — the ci-required aggregate must have no green path to false.

A stable required check is only worth having if every way a child can fail to
produce coverage -- failure, cancellation, an unauthorized skip, a missing
result, a result string GitHub has not shipped yet -- lands red. These tests
are the truth table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.aggregate_required import (
    AggregateError,
    evaluate,
    main,
    validate_configuration,
)

REQUIRED = ["lint", "test", "taskiq-smoke", "change-classifier"]


def _needs(**results: object) -> dict[str, object]:
    """Shape a `toJSON(needs)` payload."""

    return {name: {"result": value, "outputs": {}} for name, value in results.items()}


def _all_success() -> dict[str, object]:
    return _needs(**dict.fromkeys(REQUIRED, "success"))


# --------------------------------------------------------------------------
# Truth table
# --------------------------------------------------------------------------


def test_all_children_successful_is_the_only_unconditional_pass() -> None:
    verdict = evaluate(_all_success(), REQUIRED)
    assert verdict.passed is True
    assert verdict.result == "pass"
    assert verdict.failing == []


@pytest.mark.parametrize("child", REQUIRED)
@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        ("failure", "failure"),
        ("cancelled", "cancelled"),
        ("skipped", "unauthorized_skip"),
        ("neutral", "unexpected"),
        ("", "unexpected"),
        ("SUCCESS", "unexpected"),
        ("success ", "unexpected"),
        (None, "unexpected"),
    ],
)
def test_any_non_success_child_result_is_red(
    child: str, result: object, expected_status: str
) -> None:
    payload = _all_success()
    payload[child] = {"result": result, "outputs": {}}
    verdict = evaluate(payload, REQUIRED)
    assert verdict.passed is False
    failing = {c.name: c.status for c in verdict.failing}
    assert failing == {child: expected_status}


@pytest.mark.parametrize("child", REQUIRED)
def test_a_missing_child_result_is_red(child: str) -> None:
    payload = _all_success()
    del payload[child]
    verdict = evaluate(payload, REQUIRED)
    assert verdict.passed is False
    assert [(c.name, c.status) for c in verdict.failing] == [(child, "missing")]


def test_classifier_failure_alone_turns_the_aggregate_red() -> None:
    payload = _all_success()
    payload["change-classifier"] = {"result": "failure"}
    verdict = evaluate(payload, REQUIRED)
    assert verdict.passed is False


def test_cancelled_child_is_red_even_when_every_other_child_passed() -> None:
    payload = _all_success()
    payload["test"] = {"result": "cancelled"}
    verdict = evaluate(payload, REQUIRED)
    assert verdict.passed is False
    assert verdict.failing[0].status == "cancelled"


def test_only_an_explicitly_authorized_skip_is_green() -> None:
    payload = _all_success()
    payload["taskiq-smoke"] = {"result": "skipped"}

    assert evaluate(payload, REQUIRED).passed is False
    authorized = evaluate(payload, REQUIRED, authorized_skips=["taskiq-smoke"])
    assert authorized.passed is True
    assert [c.note for c in authorized.children if c.name == "taskiq-smoke"] == [
        "authorized skip"
    ]


def test_authorizing_a_skip_does_not_authorize_a_failure() -> None:
    payload = _all_success()
    payload["taskiq-smoke"] = {"result": "failure"}
    verdict = evaluate(payload, REQUIRED, authorized_skips=["taskiq-smoke"])
    assert verdict.passed is False


def test_authorize_skip_for_a_non_required_job_is_a_configuration_error() -> None:
    with pytest.raises(AggregateError, match="not required"):
        evaluate(_all_success(), REQUIRED, authorized_skips=["security"])


def test_empty_results_payload_fails_every_required_child() -> None:
    verdict = evaluate({}, REQUIRED)
    assert verdict.passed is False
    assert {c.status for c in verdict.failing} == {"missing"}


def test_an_undeclared_child_in_the_payload_is_red_by_default() -> None:
    payload = _all_success()
    payload["security"] = {"result": "success"}
    verdict = evaluate(payload, REQUIRED)
    assert verdict.passed is False
    assert [(c.name, c.status) for c in verdict.failing] == [("security", "undeclared")]
    assert evaluate(payload, REQUIRED, allow_undeclared=True).passed is True


def test_a_bare_result_string_child_is_accepted() -> None:
    payload: dict[str, object] = dict.fromkeys(REQUIRED, "success")
    assert evaluate(payload, REQUIRED).passed is True


@pytest.mark.parametrize("bad", [123, [1, 2], True])
def test_a_child_with_an_unsupported_shape_raises(bad: object) -> None:
    payload = _all_success()
    payload["lint"] = bad
    with pytest.raises(AggregateError, match="unsupported shape"):
        evaluate(payload, REQUIRED)


def test_a_child_object_with_a_non_string_result_raises() -> None:
    payload = _all_success()
    payload["lint"] = {"result": 0}
    with pytest.raises(AggregateError, match="non-string result"):
        evaluate(payload, REQUIRED)


def test_a_child_object_without_a_result_key_is_unexpected_not_pass() -> None:
    payload = _all_success()
    payload["lint"] = {"outputs": {}}
    verdict = evaluate(payload, REQUIRED)
    assert verdict.passed is False
    assert verdict.failing[0].status == "unexpected"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli(tmp_path: Path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / f"report-{len(list(tmp_path.iterdir()))}.json"
    code = main([*extra, "--json-out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def _required_flags() -> list[str]:
    flags: list[str] = []
    for name in REQUIRED:
        flags += ["--required", name]
    return flags


def test_cli_exits_zero_only_when_every_child_succeeded(tmp_path: Path) -> None:
    code, report = _cli(
        tmp_path,
        "--results-json",
        json.dumps(_all_success()),
        *_required_flags(),
    )
    assert code == 0
    assert report["result"] == "pass"
    assert report["failing"] == []


def test_cli_exits_nonzero_on_a_failed_child(tmp_path: Path) -> None:
    payload = _all_success()
    payload["test"] = {"result": "failure"}
    code, report = _cli(
        tmp_path, "--results-json", json.dumps(payload), *_required_flags()
    )
    assert code == 1
    assert report["result"] == "fail"
    assert report["failing"] == ["test"]


def test_cli_reads_the_results_from_an_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI_REQUIRED_NEEDS", json.dumps(_all_success()))
    code, report = _cli(
        tmp_path, "--results-env", "CI_REQUIRED_NEEDS", *_required_flags()
    )
    assert code == 0
    assert report["result"] == "pass"


def test_cli_is_red_when_the_results_env_var_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI_REQUIRED_NEEDS", raising=False)
    code, report = _cli(
        tmp_path, "--results-env", "CI_REQUIRED_NEEDS", *_required_flags()
    )
    assert code == 1
    assert "not set" in str(report["error"])


@pytest.mark.parametrize("payload", ["", "   ", "not json", "[]", '"success"', "null"])
def test_cli_is_red_on_a_malformed_results_payload(
    tmp_path: Path, payload: str
) -> None:
    code, report = _cli(tmp_path, "--results-json", payload, *_required_flags())
    assert code == 1
    assert report["result"] == "fail"
    assert report["error"]


def test_cli_refuses_to_run_without_any_required_names(tmp_path: Path) -> None:
    """An aggregate with an empty required list would pass vacuously."""

    code, report = _cli(tmp_path, "--results-json", json.dumps(_all_success()))
    assert code == 1
    assert "vacuously" in str(report["error"])


def test_cli_requires_exactly_one_results_source(tmp_path: Path) -> None:
    code, report = _cli(
        tmp_path,
        "--results-json",
        "{}",
        "--results-file",
        str(tmp_path / "x.json"),
        *_required_flags(),
    )
    assert code == 1
    assert "exactly one" in str(report["error"])


def test_cli_report_records_every_child_verdict(tmp_path: Path) -> None:
    payload = _all_success()
    payload["lint"] = {"result": "cancelled"}
    _, report = _cli(
        tmp_path, "--results-json", json.dumps(payload), *_required_flags()
    )
    statuses = {child["name"]: child["status"] for child in report["children"]}
    assert statuses == {
        "lint": "cancelled",
        "test": "pass",
        "taskiq-smoke": "pass",
        "change-classifier": "pass",
    }


# --------------------------------------------------------------------------
# ROB-1294 verifier P2 — a malformed gate declaration must never reach a
# verdict. Both CLI cases below exited 0 with result=pass before the fix.
# --------------------------------------------------------------------------


def test_a_blank_required_name_is_a_configuration_error() -> None:
    with pytest.raises(AggregateError, match="blank or whitespace-only"):
        evaluate({"": {"result": "success"}}, [""])


@pytest.mark.parametrize("name", ["", " ", "\t", "\n"])
def test_every_blank_shape_of_required_name_is_rejected(name: str) -> None:
    with pytest.raises(AggregateError, match="blank or whitespace-only"):
        validate_configuration([name])


def test_a_duplicate_required_name_is_a_configuration_error() -> None:
    with pytest.raises(AggregateError, match="duplicate job name 'lint'"):
        evaluate(_all_success(), ["lint", "lint", "test"])


def test_a_blank_authorize_skip_name_is_a_configuration_error() -> None:
    with pytest.raises(AggregateError, match="blank or whitespace-only"):
        evaluate(_all_success(), REQUIRED, authorized_skips=[""])


def test_a_duplicate_authorize_skip_name_is_a_configuration_error() -> None:
    with pytest.raises(AggregateError, match="duplicate job name 'lint'"):
        evaluate(_all_success(), REQUIRED, authorized_skips=["lint", "lint"])


def test_an_empty_required_list_is_a_configuration_error() -> None:
    with pytest.raises(AggregateError, match="vacuously"):
        validate_configuration([])


def test_validate_configuration_accepts_the_shipped_workflow_declaration() -> None:
    validate_configuration(REQUIRED)


def test_cli_is_red_on_a_blank_required_name(tmp_path: Path) -> None:
    code, report = _cli(
        tmp_path, "--results-json", '{"":{"result":"success"}}', "--required", ""
    )
    assert code == 1
    assert report["result"] == "fail"
    assert "blank or whitespace-only" in str(report["error"])


def test_cli_is_red_on_a_duplicated_required_name(tmp_path: Path) -> None:
    code, report = _cli(
        tmp_path,
        "--results-json",
        json.dumps(_all_success()),
        "--required",
        "lint",
        "--required",
        "lint",
        "--required",
        "test",
        "--required",
        "taskiq-smoke",
        "--required",
        "change-classifier",
    )
    assert code == 1
    assert report["result"] == "fail"
    assert "duplicate job name 'lint'" in str(report["error"])


def test_cli_is_red_on_a_blank_authorize_skip_name(tmp_path: Path) -> None:
    code, report = _cli(
        tmp_path,
        "--results-json",
        json.dumps(_all_success()),
        *_required_flags(),
        "--authorize-skip",
        "",
    )
    assert code == 1
    assert "blank or whitespace-only" in str(report["error"])


def test_a_malformed_declaration_is_rejected_before_the_payload_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate declaration is validated first, so an unreadable payload
    cannot mask -- or be masked by -- a bad required list."""

    monkeypatch.delenv("CI_REQUIRED_NEEDS", raising=False)
    code, report = _cli(
        tmp_path,
        "--results-env",
        "CI_REQUIRED_NEEDS",
        "--required",
        "",
        "--required",
        "",
    )
    assert code == 1
    assert "blank or whitespace-only" in str(report["error"])
