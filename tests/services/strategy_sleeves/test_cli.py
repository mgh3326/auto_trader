"""ROB-1470 operator CLI: every amount is an explicit argument (no database)."""

from __future__ import annotations

import pytest

from scripts import strategy_sleeves as cli

pytestmark = pytest.mark.unit

_DB = ["--database-url-env", "SLEEVE_TEST_DB"]
_CREATE = [
    "create",
    "--sleeve-key",
    "k",
    "--rule-id",
    "rob1458.us_near_market_entry",
    "--account-mode",
    "kis_live",
    "--market",
    "equity_us",
    "--state",
    "shadow",
    "--reason",
    "r",
    "--actor",
    "op",
]


@pytest.mark.parametrize("missing", ["--budget-cap", "--loss-limit"])
def test_create_has_no_default_amount(missing) -> None:
    args = [*_DB, *_CREATE, "--budget-cap", "1", "--loss-limit", "1"]
    index = args.index(missing)
    del args[index : index + 2]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(args)


def test_create_and_set_preview_unless_commit() -> None:
    created = cli._parser().parse_args(
        [*_DB, *_CREATE, "--budget-cap", "1", "--loss-limit", "1"]
    )
    assert created.commit is False
    updated = cli._parser().parse_args(
        [
            *_DB,
            "set",
            "--sleeve-key",
            "k",
            "--state",
            "live",
            "--reason",
            "r",
            "--actor",
            "op",
        ]
    )
    assert updated.commit is False
    assert (updated.budget_cap, updated.loss_limit) == (None, None)


def test_a_new_sleeve_cannot_start_stopped() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                *_DB,
                *_CREATE[:-6],
                "--state",
                "stopped",
                "--reason",
                "r",
                "--actor",
                "op",
                "--budget-cap",
                "1",
                "--loss-limit",
                "1",
            ]
        )


def test_the_database_url_value_is_never_echoed(monkeypatch) -> None:
    monkeypatch.setenv("SLEEVE_TEST_DB", "not a url secret-value")
    args = cli._parser().parse_args([*_DB, "report"])
    with pytest.raises(ValueError) as caught:
        cli._database_url(args)
    assert "secret-value" not in str(caught.value)
    assert "SLEEVE_TEST_DB" in str(caught.value)
