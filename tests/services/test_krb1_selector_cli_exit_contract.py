"""M03 anchor — the CLI exit code is the outermost fail-close contract.

The adversarial review of #1729 changed the CLI's exit code to always return 0 and
the whole suite still passed: 24 passed. Nothing protected the property an operator
and any wrapper script actually observe. These tests pin it directly.

No database, no network: ``run`` is replaced with a stub, so only the exit-code
decision is under test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from scripts import krb1_p0_liquidity_selector as cli

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
ARGV = [
    "--as-of-session",
    "2026-07-29",
    "--target-session",
    "2026-07-30",
    "--decision-at",
    "2026-07-29T18:00:00+09:00",
]


def _stub_run(result: dict[str, Any]):
    async def _run(**_kwargs: object) -> dict[str, Any]:
        return result

    return _run


def test_fail_closed_exits_2(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """🔴 M03: a fail-closed run must exit 2, never 0."""
    monkeypatch.setattr(
        cli,
        "run",
        _stub_run(
            {
                "status": "fail_closed",
                "selected_candidates": [],
                "fallback_used": False,
            }
        ),
    )

    exit_code = asyncio.run(cli.main(ARGV))

    assert exit_code == 2
    payload = capsys.readouterr().out
    assert '"status": "fail_closed"' in payload


def test_selected_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "run",
        _stub_run(
            {
                "status": "selected",
                "selected_candidates": [{"market": "KOSPI"}, {"market": "KOSDAQ"}],
                "fallback_used": False,
            }
        ),
    )

    assert asyncio.run(cli.main(ARGV)) == 0


@pytest.mark.parametrize(
    "status",
    ["fail_closed", "unprovable", "error", "", "SELECTED", "selected_but_not_really"],
)
def test_only_the_exact_selected_status_exits_0(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Anything that is not exactly ``selected`` must exit non-zero."""
    monkeypatch.setattr(
        cli, "run", _stub_run({"status": status, "selected_candidates": []})
    )

    assert asyncio.run(cli.main(ARGV)) == 2


def test_an_exception_inside_run_still_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A crash must not be reported as success either."""

    async def _boom(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(cli, "run", _boom)

    exit_code = asyncio.run(cli.main(ARGV))

    assert exit_code == 2
    payload = capsys.readouterr().out
    assert "selector_input_data_unprovable" in payload
    assert '"status": "fail_closed"' in payload


def test_decision_at_is_required_and_must_carry_an_offset() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--as-of-session", "2026-07-29"])
    with pytest.raises(SystemExit):
        cli.parse_args(
            ["--as-of-session", "2026-07-29", "--decision-at", "2026-07-29T18:00:00"]
        )
    args = cli.parse_args(ARGV)
    assert args.decision_at == dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
