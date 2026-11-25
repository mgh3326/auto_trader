"""hk 6508: CLI wiring with a mock transport over saved fixtures (no network, no DB)."""

from __future__ import annotations

import json

import httpx
import pytest

import scripts.collect_fnguide_consensus as cli
from app.services.fnguide_consensus.client import FnguideClient
from tests.services.fnguide_consensus._helpers import load_fixture

pytestmark = pytest.mark.unit

_FIXTURES = {
    ("005930", "Consensus"): "005930_consensus.html",
    ("005930", "Invest"): "005930_invest.html",
    ("000660", "Consensus"): "000660_consensus.html",
    ("028300", "Consensus"): "028300_consensus.html",
}


def _handler(request: httpx.Request) -> httpx.Response:
    page = request.url.path.rsplit("/", 1)[-1]
    code = request.url.params.get("cmp_cd", "")
    name = _FIXTURES.get((code, page))
    if name is None:
        return httpx.Response(404)
    return httpx.Response(200, text=load_fixture(name))


@pytest.fixture
def mock_transport(monkeypatch):
    def factory(**kwargs):
        kwargs.pop("interval", None)
        return FnguideClient(
            transport=httpx.MockTransport(_handler),
            min_interval=0.0,
            concurrency=kwargs.get("concurrency", 2),
            retries=kwargs.get("retries", 0),
            timeout=kwargs.get("timeout", 5.0),
        )

    monkeypatch.setattr(cli, "FnguideClient", factory)


def _run(capsys, argv):
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_dry_run_with_explicit_symbols_writes_nothing_and_reports(
    capsys, mock_transport
) -> None:
    code, report = _run(capsys, ["--symbols", "005930,000660", "--pages", "Consensus"])

    assert code == 0
    assert report["mode"] == "dry-run"
    assert report["universe"] == "explicit"
    assert report["written"] == []
    assert report["totals"]["parsed_pages"] == 2
    assert report["failures"] == []


def test_failed_page_is_reported_by_kind_and_status_and_exits_one(
    capsys, mock_transport
) -> None:
    code, report = _run(capsys, ["--symbols", "028300", "--pages", "Consensus,Invest"])

    assert code == 1
    assert report["failures"] == [
        {"symbol": "028300", "page": "Invest", "kind": "http_4xx", "http_status": 404}
    ]
    parsed = {(p["symbol"], p["page"]): p for p in report["parsed"]}
    assert parsed[("028300", "Consensus")]["reason"] == "all_consensus_values_null"


def test_bad_symbol_is_a_usage_error(capsys) -> None:
    assert cli.main(["--symbols", "12345"]) == 2


def test_unknown_page_is_a_usage_error(capsys) -> None:
    assert cli.main(["--symbols", "005930", "--pages", "Nope"]) == 2


def test_concurrency_above_two_is_a_usage_error(capsys) -> None:
    assert cli.main(["--symbols", "005930", "--concurrency", "3"]) == 2


def test_dry_run_and_commit_are_mutually_exclusive(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--symbols", "005930", "--dry-run", "--commit"])
