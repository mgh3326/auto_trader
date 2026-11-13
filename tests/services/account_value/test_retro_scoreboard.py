import ast
import copy
import inspect
import json
import re
from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource
from sqlalchemy.sql.elements import TextClause

from app.services.account_value import load_report, retro_scoreboard
from app.services.account_value.report import ReportRow, render
from app.services.account_value.retro_scoreboard import read_retro_scoreboard
from scripts import account_value_report

ROOT = Path(__file__).resolve().parents[3]
SPEC = yaml.safe_load((ROOT / "contracts/retro/v1/openapi.yaml").read_text())
REGISTRY = Registry().with_resource(
    "urn:retro",
    Resource.from_contents(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", **SPEC}
    ),
)
VALIDATOR = jsonschema.Draft202012Validator(
    {"$ref": "#/components/schemas/RetroScoreboardResponse"},
    registry=REGISTRY,
    _resolver=REGISTRY.resolver("urn:retro"),
    format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
)

START = date(2026, 1, 1)
END = START + timedelta(days=7)
ACCOUNT = "upbit:upbit:" + "a" * 64
SLEEVE_KEY = "alpha-secret-sleeve-name"
OBSERVED = datetime(2026, 1, 8, 9, 30, tzinfo=UTC)


class Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class Database:
    """Fake read-only session: records SQL text, supports only reads."""

    def __init__(self, results=()):
        self.results = iter(results)
        self.texts = []
        self.params = []

    async def execute(self, statement, params=None):
        if isinstance(statement, TextClause):
            self.texts.append(str(statement))
            self.params.append(params)
            return Result("on")
        return Result(next(self.results))


def snapshots():
    return [
        SimpleNamespace(
            account=ACCOUNT,
            market="account",
            day=START + timedelta(days=i),
            holdings=[],
            cash={"KRW": "100"},
            reasons=[],
        )
        for i in range(8)
    ]


def external_flow(day_offset=3):
    return SimpleNamespace(
        account=ACCOUNT,
        market="account",
        day=START + timedelta(days=day_offset),
        currency="KRW",
        amount=Decimal("50"),
    )


async def build(monkeypatch, flows=(), **kwargs):
    async def no_sleeves(_session):
        return []

    async def no_prices(*args):
        return None, None

    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    monkeypatch.setattr(load_report, "_benchmark", no_prices)
    database = Database(
        [[], snapshots(), list(flows), [(ACCOUNT, "account")], OBSERVED]
    )
    result = await read_retro_scoreboard(
        database, **{"from": START, "to": END}, **kwargs
    )
    return database, result


def null_paths(value, path):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from null_paths(child, f"{path}.{key}")
    elif value is None:
        yield path


def assert_nulls_explained(response):
    unknowns = {item["field"] for item in response["unknowns"]}
    for index, row in enumerate(response["data"]):
        for path in null_paths(row, f"data.{index}"):
            assert path in unknowns, path


def synthetic_rows():
    return [
        ReportRow(
            ACCOUNT,
            "account",
            None,
            Decimal("100"),
            Decimal("110.50"),
            Decimal("0"),
            Decimal("1E-7"),
            None,
            ("account_benchmark_unavailable",),
            "modified_dietz_account_krw",
            "-",
        ),
        ReportRow(
            "toss:toss_live:" + "b" * 64,
            "us",
            None,
            Decimal("5"),
            Decimal("6"),
            Decimal("0"),
            Decimal("0.2"),
            None,
            ("benchmark_total_return_unverified",),
            "holdings_only",
        ),
        ReportRow(
            "kis:kis_live:" + "c" * 64,
            "kr",
            SLEEVE_KEY,
            None,
            None,
            None,
            Decimal("-0.01"),
            Decimal("0.03"),
            (),
            "sleeve_attributed_pnl_over_budget",
        ),
        ReportRow(
            "upbit:upbit:" + "d" * 64,
            "crypto",
            None,
            None,
            None,
            None,
            None,
            Decimal(0),
            ("flow_incomplete:SECRETSYM", "missing_snapshot_day"),
            "holdings_only",
        ),
    ]


@pytest.fixture
def synthetic_loader(monkeypatch):
    calls = []

    async def load(session, start, end, **kwargs):
        calls.append((start, end, kwargs))
        return synthetic_rows()

    monkeypatch.setattr(retro_scoreboard, "load_weekly_report", load)
    return calls


@pytest.mark.asyncio
async def test_output_validates_against_contract(monkeypatch):
    _, response = await build(monkeypatch)
    VALIDATOR.validate(response)
    assert_nulls_explained(response)
    assert response["total"] == 1 and response["truncated"] is False
    assert response["data_as_of"] == "2026-01-08T09:30:00.000000Z"
    assert response["limit"] == 100 and response["cursor"] is None


def test_validator_rejects_missing_as_of_and_float_rate():
    row = {
        "scope": "account",
        "subject_ref": "ref_x",
        "market": "account",
        "basis": "modified_dietz",
        "evidence_basis": "broker",
        "return_rate": "0.1",
        "unmeasured_reason": None,
        "flows_complete": False,
        "start_amount": "1",
        "end_amount": "1",
        "flow_amount": "0",
        "currency": "KRW",
        "unit": "currency",
        "benchmark": {
            "return_rate": None,
            "source": None,
            "basis": "unknown",
            "unknown_reason": "not_applicable",
        },
    }
    good = {
        "as_of": "2026-01-08T00:00:00Z",
        "data_as_of": None,
        "timezone": "UTC",
        "total": 1,
        "truncated": False,
        "limit": 100,
        "cursor": None,
        "next_cursor": None,
        "unknowns": [],
        "data": [row],
    }
    VALIDATOR.validate(good)
    missing = copy.deepcopy(good)
    del missing["as_of"]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(missing)
    floated = copy.deepcopy(good)
    floated["data"][0]["return_rate"] = 0.1
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(floated)


@pytest.mark.asyncio
async def test_unmeasured_account_row_carries_reason_and_server_side_flag(
    monkeypatch,
):
    _, response = await build(monkeypatch, flows=[external_flow()])
    (row,) = response["data"]
    assert row["scope"] == "account" and row["basis"] == "modified_dietz"
    assert row["return_rate"] is None
    assert "flows_not_confirmed" in row["unmeasured_reason"].split(".")
    assert row["flows_complete"] is False
    assert row["evidence_basis"] == "unknown"
    assert {
        "field": "data.0.return_rate",
        "reason": "flows_not_confirmed",
    } in response["unknowns"]


@pytest.mark.asyncio
async def test_flows_complete_is_never_an_input(monkeypatch, synthetic_loader):
    parameters = inspect.signature(read_retro_scoreboard).parameters
    assert not [name for name in parameters if "flow" in name.lower()]
    assert [p.name for p in parameters.values() if p.kind is p.KEYWORD_ONLY] == [
        "limit",
        "cursor",
    ]
    database = Database([[], None])
    for extra in (
        {"flows_complete": True},
        {"flows_confirmed": True},
        {"flows": True},
        {"x_flows_complete": "true"},
    ):
        with pytest.raises(TypeError):
            await read_retro_scoreboard(database, **{"from": START, "to": END}, **extra)
    assert synthetic_loader == []
    await read_retro_scoreboard(Database([[], None]), **{"from": START, "to": END})
    assert synthetic_loader == [(START, END, {"confirmations": []})]


@pytest.mark.asyncio
async def test_rows_keep_bases_reasons_and_benchmarks(synthetic_loader):
    response = await read_retro_scoreboard(
        Database([[], None]), **{"from": START, "to": END}
    )
    VALIDATOR.validate(response)
    assert_nulls_explained(response)
    by_scope = {(row["scope"], row["market"]): row for row in response["data"]}
    assert [row["scope"] for row in response["data"]] == [
        "account",
        "market",
        "market",
        "sleeve",
    ]
    account = by_scope["account", "account"]
    assert account["basis"] == "modified_dietz" and account["flows_complete"] is False
    assert account["return_rate"] == "0.0000001"
    assert account["start_amount"] == "100" and account["end_amount"] == "110.5"
    assert account["benchmark"]["unknown_reason"] == "not_applicable"
    us = by_scope["market", "us"]
    assert us["basis"] == "holdings_only" and us["flows_complete"] is None
    assert us["return_rate"] == "0.2" and us["unmeasured_reason"] is None
    assert us["benchmark"] == {
        "return_rate": None,
        "source": "voo_adjusted_close",
        "basis": "unknown",
        "unknown_reason": "source_mixed",
    }
    crypto = by_scope["market", "crypto"]
    assert crypto["return_rate"] is None
    assert crypto["unmeasured_reason"] == "flow_incomplete.missing_snapshot_day"
    assert crypto["benchmark"]["return_rate"] == "0"
    sleeve = by_scope["sleeve", "kr"]
    assert sleeve["basis"] == "sleeve_budget" and sleeve["return_rate"] == "-0.01"
    assert sleeve["evidence_basis"] == "inference"
    assert sleeve["benchmark"]["return_rate"] == "0.03"
    assert sleeve["benchmark"]["source"] == "krx_069500_close"
    assert sleeve["flows_complete"] is None
    assert sleeve["start_amount"] is None and sleeve["currency"] == "KRW"


@pytest.mark.asyncio
async def test_no_float_and_no_identity_leaves_the_reader(synthetic_loader):
    response = await read_retro_scoreboard(
        Database([[], None]), **{"from": START, "to": END}
    )
    encoded = json.dumps(response)

    def values(node):
        if isinstance(node, dict):
            for child in node.values():
                yield from values(child)
        elif isinstance(node, list):
            for child in node:
                yield from values(child)
        else:
            yield node

    assert not [v for v in values(response) if isinstance(v, float)]
    for secret in ("a" * 64, "b" * 64, "c" * 64, "d" * 64, SLEEVE_KEY, "SECRETSYM"):
        assert secret not in encoded
    for broker in ("upbit", "toss", "kis_live", "toss_live"):
        assert broker not in encoded
    refs = [row["subject_ref"] for row in response["data"]]
    assert all(re.fullmatch(r"ref_[0-9a-f]{32}", ref) for ref in refs)
    assert len(set(refs)) == len(refs)
    again = await read_retro_scoreboard(
        Database([[], None]), **{"from": START, "to": END}
    )
    assert [row["subject_ref"] for row in again["data"]] == refs


@pytest.mark.asyncio
async def test_read_only_transaction_and_pagination(synthetic_loader):
    database = Database([[], OBSERVED])
    response = await read_retro_scoreboard(
        database, limit=3, **{"from": START, "to": END}
    )
    assert (
        database.texts[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert database.texts[1] == "SHOW transaction_read_only"
    assert "statement_timeout" in database.texts[2]
    assert database.params[2] == {"value": "8000"}
    assert response["total"] == 4 and len(response["data"]) == 3
    assert response["truncated"] is True and response["next_cursor"] == "o3"
    VALIDATOR.validate(response)
    last = await read_retro_scoreboard(
        Database([[], OBSERVED]), limit=3, cursor="o3", **{"from": START, "to": END}
    )
    assert len(last["data"]) == 1 and last["next_cursor"] is None
    assert last["cursor"] == "o3"
    VALIDATOR.validate(last)


def test_module_imports_no_writer_or_refresh_path():
    tree = ast.parse(Path(retro_scoreboard.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not [
        name
        for name in imported
        if name
        and re.search(
            r"account_value\.(snapshot|flows|readers|broker_views)|brokers|order",
            name,
        )
    ]
    source = Path(retro_scoreboard.__file__).read_text()
    for forbidden in (".commit(", ".add(", ".flush(", "insert(", "update(", "delete("):
        assert forbidden not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"from": date(2026, 1, 2), "to": date(2026, 1, 1)},
        {"from": date(2025, 1, 1), "to": date(2026, 1, 2)},
        {"from": "garbage", "to": date(2026, 1, 2)},
        {"from": date(2026, 1, 1), "to": date(2026, 1, 2), "limit": 0},
        {"from": date(2026, 1, 1), "to": date(2026, 1, 2), "limit": 501},
        {"from": date(2026, 1, 1), "to": date(2026, 1, 2), "cursor": "bad cursor"},
        {"from": datetime(2026, 1, 1, tzinfo=UTC), "to": date(2026, 1, 2)},
    ],
)
async def test_invalid_parameters_are_rejected_before_any_sql(params):
    database = Database([])
    with pytest.raises(ValueError):
        await read_retro_scoreboard(database, **params)
    assert database.texts == []


@pytest.mark.asyncio
async def test_366_day_span_is_accepted_and_367_rejected(synthetic_loader):
    start = date(2024, 1, 1)
    response = await read_retro_scoreboard(
        Database([[], None]), **{"from": start, "to": start + timedelta(days=365)}
    )
    VALIDATOR.validate(response)
    with pytest.raises(ValueError):
        await read_retro_scoreboard(
            Database([[], None]), **{"from": start, "to": start + timedelta(days=366)}
        )


@pytest.mark.asyncio
async def test_operator_cli_render_is_unchanged(monkeypatch, capsys, synthetic_loader):
    rows = synthetic_rows()
    expected = render(rows)
    expected_lines = [
        "account | market | sleeve | start | end | flows | return | benchmark | excess | basis | fx | unmeasured",
        f"kis:kis_live:{'c' * 64} | kr | {SLEEVE_KEY} | unknown | unknown | unknown | -1.00% | 3.00% | -4.00% | sleeve_attributed_pnl_over_budget | - | -",
        f"toss:toss_live:{'b' * 64} | us | - | 5 | 6 | 0 | 20.00% | unknown | unknown | holdings_only | - | benchmark_total_return_unverified",
        f"upbit:upbit:{'a' * 64} | account | - | 100 | 110.50 | 0 | 0.00% | unknown | unknown | modified_dietz_account_krw | - | account_benchmark_unavailable",
        f"upbit:upbit:{'d' * 64} | crypto | - | unknown | unknown | unknown | unknown | 0.00% | unknown | holdings_only | - | flow_incomplete:SECRETSYM, missing_snapshot_day",
    ]
    assert expected == "\n".join(expected_lines)

    class Context:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            pass

    seen = []

    async def load(session, start, end, **kwargs):
        seen.append(kwargs)
        return rows

    # The CLI keeps its own loader call, including the operator flag.
    monkeypatch.setattr(account_value_report, "load_weekly_report", load)
    monkeypatch.setattr(account_value_report, "session_factory", lambda: Context())
    await read_retro_scoreboard(Database([[], None]), **{"from": START, "to": END})
    for flag in (False, True):
        await account_value_report.run(
            Namespace(start=START, end=END, flows_complete=flag)
        )
        assert capsys.readouterr().out == expected + "\n"
    assert seen == [{"flows_complete": False}, {"flows_complete": True}]
    assert synthetic_rows() == rows


@pytest.mark.asyncio
async def test_flows_not_confirmed_survives_a_long_reason_list(monkeypatch):
    async def load(session, start, end, **kwargs):
        reasons = tuple(
            f"{name}_{'x' * 20}" for name in ("account_identity_unknown", "cash_dup")
        ) + ("a_" + "y" * 30, "b_" + "z" * 30, "flows_not_confirmed", "zz_" + "q" * 40)
        return [
            ReportRow(
                ACCOUNT,
                "account",
                None,
                None,
                None,
                None,
                None,
                None,
                reasons,
                "modified_dietz_account_krw",
            )
        ]

    monkeypatch.setattr(retro_scoreboard, "load_weekly_report", load)
    response = await read_retro_scoreboard(
        Database([[], None]), **{"from": START, "to": END}
    )
    VALIDATOR.validate(response)
    reason = response["data"][0]["unmeasured_reason"]
    assert len(reason) <= 128
    assert reason.split(".")[0] == "flows_not_confirmed"
    assert len(reason.split(".")) < 6


@pytest.mark.asyncio
async def test_subject_ref_is_keyed(monkeypatch, synthetic_loader):
    from app.core.config import settings

    first = await read_retro_scoreboard(
        Database([[], None]), **{"from": START, "to": END}
    )
    monkeypatch.setattr(settings, "SECRET_KEY", "another-key-for-the-test")
    second = await read_retro_scoreboard(
        Database([[], None]), **{"from": START, "to": END}
    )
    assert [r["subject_ref"] for r in first["data"]] != [
        r["subject_ref"] for r in second["data"]
    ]


@pytest.mark.asyncio
async def test_server_confirmation_drives_account_row(monkeypatch):
    async def no_sleeves(_session):
        return []

    async def no_prices(*args):
        return None, None

    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    monkeypatch.setattr(load_report, "_benchmark", no_prices)
    confirmed_at = datetime(2026, 1, 9, tzinfo=UTC)
    confirmation = SimpleNamespace(
        account=ACCOUNT, start_day=START, end_day=END, confirmed_at=confirmed_at
    )
    for flows, expected in (
        ([], True),
        (
            [
                SimpleNamespace(
                    account=ACCOUNT,
                    market="account",
                    day=START + timedelta(days=3),
                    currency="KRW",
                    amount=Decimal("10"),
                    created_at=confirmed_at + timedelta(seconds=1),
                )
            ],
            False,
        ),
    ):
        database = Database(
            [[confirmation], snapshots(), flows, [(ACCOUNT, "account")], OBSERVED]
        )
        response = await read_retro_scoreboard(database, **{"from": START, "to": END})
        row = response["data"][0]
        assert row["flows_complete"] is expected
        if expected:
            assert row["return_rate"] == "0"
        else:
            assert row["return_rate"] is None
            assert "flows_not_confirmed" in row["unmeasured_reason"]


@pytest.mark.asyncio
async def test_confirmation_is_account_scoped_and_other_rows_unchanged(monkeypatch):
    other = "toss:toss_live:" + "b" * 64
    first_snapshots = snapshots()
    second_snapshots = [
        SimpleNamespace(**{**vars(item), "account": other}) for item in snapshots()
    ]

    async def no_sleeves(_session):
        return []

    async def no_prices(*args):
        return None, None

    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    monkeypatch.setattr(load_report, "_benchmark", no_prices)
    confirmation = SimpleNamespace(
        account=ACCOUNT, start_day=START, end_day=END, confirmed_at=OBSERVED
    )

    async def read(records):
        db = Database(
            [
                records,
                first_snapshots + second_snapshots,
                [],
                [(ACCOUNT, "account"), (other, "account")],
                OBSERVED,
            ]
        )
        return await read_retro_scoreboard(db, **{"from": START, "to": END})

    before, after = await read([]), await read([confirmation])
    by_ref = {row["subject_ref"]: row for row in after["data"]}
    assert by_ref[retro_scoreboard._ref(ACCOUNT)]["flows_complete"] is True
    assert by_ref[retro_scoreboard._ref(other)]["flows_complete"] is False
    assert by_ref[retro_scoreboard._ref(other)] == next(
        row
        for row in before["data"]
        if row["subject_ref"] == retro_scoreboard._ref(other)
    )
    for row in synthetic_rows()[1:]:
        assert retro_scoreboard._score_row(row, False) == retro_scoreboard._score_row(
            row, True
        )
