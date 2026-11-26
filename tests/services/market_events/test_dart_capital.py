"""Offline OpenDART guide projections with deterministic arithmetic inputs."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, Mock
from zipfile import ZipFile

import pytest

from app.models.market_events import MarketEventValue
from app.services.market_events import dart_capital, ingestion
from app.services.market_events.query_service import (
    MarketEventsQueryService,
    _value_response,
)

RECEIPT = "20261124000001"
CORP = "00123456"
FILING = {
    "rcept_no": RECEIPT,
    "rcept_dt": "20261124",
    "corp_code": CORP,
    "corp_name": "테스트",
    "stock_code": "005930",
    "report_nm": "[기재정정]주요사항보고서(전환사채권발행결정)",
}
# OpenDART DS005/2020033 response properties. Numeric examples chosen to
# exercise both rounding-free arithmetic and conversion-price/floor separation.
CB = {
    "bd_fta": "2,500,000",
    "cv_prc": "1,000",
    "cvisstk_cnt": "2,500",
    "cvisstk_tisstk_vs": "6.25",
    "act_mktprcfl_cvprc_lwtrsprc": "500",
    "act_mktprcfl_cvprc_lwtrsprc_bs": "최저 조정가액 근거",
}


def response(detail):
    return json.dumps(
        {
            "status": "000",
            "list": [{"rcept_no": RECEIPT, "corp_code": CORP, **detail}],
        }
    ).encode()


@pytest.mark.asyncio
async def test_cb_normal_ingestion_retries_and_stores_exact_dilution(monkeypatch):
    transport = Mock(side_effect=[b'{"status":"020"}', response(CB)])
    delay = AsyncMock()
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    monkeypatch.setattr(dart_capital.asyncio, "sleep", delay)
    monkeypatch.setattr(
        "app.services.market_events.dart_helpers.fetch_dart_filings_for_date",
        AsyncMock(return_value=[FILING]),
    )
    repo = AsyncMock()
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))

    result = await ingestion.ingest_kr_disclosures_for_date(
        AsyncMock(), date(2026, 11, 24)
    )

    assert result.status == "succeeded"
    assert result.event_count == 1
    delay.assert_awaited_once_with(1)
    assert transport.call_count == 2
    endpoint, params = transport.call_args.args
    assert endpoint == "cvbdIsDecsn.json"
    assert params["corp_code"] == CORP
    assert (
        date.fromisoformat(params["end_de"]) - date.fromisoformat(params["bgn_de"])
    ).days == 365
    event, values = repo.upsert_event_with_values.await_args.args
    assert event["source_event_id"] == RECEIPT
    assert (
        event["raw_payload_json"]["dart_reset_floor_basis"]
        == CB["act_mktprcfl_cvprc_lwtrsprc_bs"]
    )
    dilution = next(
        value for value in values if value["metric_name"] == "worst_case_dilution"
    )
    assert dilution["value_details"] == {"actual": "12.500"}
    model = MarketEventValue(**dilution)
    assert _value_response(model).actual == Decimal("12.5")


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("bd_fta", "face_amount"),
        ("act_mktprcfl_cvprc_lwtrsprc", "reset_floor"),
        ("cvisstk_tisstk_vs", "shares_outstanding"),
    ],
)
def test_missing_dilution_input_is_null_with_reason(missing, reason):
    values = dart_capital.normalize_capital_detail("cvbdIsDecsn", {**CB, missing: "-"})
    assert values["worst_case_dilution"] == {
        "actual": None,
        "unit": "%",
        "reason": f"missing_or_invalid_{reason}",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "detail", "expected_date"),
    [
        (
            "majorstock",
            {"stkrt": "7.10", "stkrt_irds": "-0.50", "rcept_dt": "20261124"},
            date(2026, 11, 24),
        ),
        (
            "elestock",
            {
                "sp_stock_lmp_rate": "7.10",
                "sp_stock_lmp_irds_rate": "-0.50",
                "rcept_dt": "20261125",
            },
            date(2026, 11, 25),
        ),
    ],
)
async def test_ownership_uses_exact_receipt_and_later_insider_date(
    monkeypatch, endpoint, detail, expected_date
):
    event = {
        "source_event_id": RECEIPT,
        "event_date": date(2026, 11, 24),
        "company_name": "테스트",
        "symbol": "005930",
        "raw_payload_json": {},
    }
    body = json.loads(response(detail))
    body["list"].insert(0, {**body["list"][0], "rcept_no": "20261124000002"})
    transport = Mock(return_value=json.dumps(body).encode())
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)

    values = await dart_capital.enrich_capital_event(endpoint, FILING, event, {}, {})

    transport.assert_called_once_with(f"{endpoint}.json", {"corp_code": CORP})
    assert event["event_date"] == expected_date
    assert {
        value["metric_name"]: value["value_details"]["actual"] for value in values
    } == {
        "holder_ratio_before": "7.60",
        "holder_ratio_after": "7.10",
        "holder_ratio_change": "-0.50",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"), [("013", "succeeded"), ("011", "failed")]
)
async def test_empty_detail_leaves_filing_and_refused_credentials_fail_run(
    monkeypatch, status, expected
):
    monkeypatch.setattr(
        dart_capital,
        "_get_bytes",
        Mock(return_value=json.dumps({"status": status}).encode()),
    )
    repo, db = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))
    result = await ingestion.ingest_kr_disclosures_for_date(
        db,
        date(2026, 11, 24),
        fetch_rows=AsyncMock(return_value=[FILING]),
        enrich_capital=True,
    )
    assert result.status == expected
    if status == "013":
        assert repo.upsert_event_with_values.await_args.args[1] == []
        db.rollback.assert_not_awaited()
    else:
        repo.upsert_event_with_values.assert_awaited_once()
        db.rollback.assert_not_awaited()
        assert result.error == "OpenDART detail status 011"


# OpenDART DS005, one response-field projection for each remaining endpoint.
@pytest.mark.parametrize(
    ("title", "endpoint", "detail", "metric", "expected"),
    [
        (
            "유상증자결정",
            "piicDecsn",
            {"nstk_ostk_cnt": "9,999"},
            "new_shares_common",
            "9999",
        ),
        (
            "무상증자결정",
            "fricDecsn",
            {"bfic_tisstk_estk": "9,999"},
            "shares_before_preferred",
            "9999",
        ),
        (
            "감자결정",
            "crDecsn",
            {"crstk_ostk_cnt": "9,999"},
            "reduced_shares_common",
            "9999",
        ),
        (
            "신주인수권부사채권발행결정",
            "bdwtIsDecsn",
            {"ex_prc": "9,999"},
            "conversion_price",
            "9999",
        ),
        (
            "교환사채권발행결정",
            "exbdIsDecsn",
            {"extg_stkcnt": "9,999"},
            "exchange_shares",
            "9999",
        ),
        (
            "자기주식취득결정",
            "tsstkAqDecsn",
            {"aqpln_prc_ostk": "9,999", "aqpln_stk_ostk": "10"},
            "buyback_amount_common",
            "9999",
        ),
        (
            "자기주식처분결정",
            "tsstkDpDecsn",
            {"dppln_stk_estk": "9,999"},
            "treasury_sale_shares_preferred",
            "9999",
        ),
    ],
)
@pytest.mark.asyncio
async def test_decision_response_fields(
    monkeypatch, title, endpoint, detail, metric, expected
):
    transport = Mock(return_value=response(detail))
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    event = {
        "source_event_id": RECEIPT,
        "event_date": date(2026, 11, 24),
        "company_name": "테스트",
        "symbol": "005930",
        "raw_payload_json": {},
    }
    assert dart_capital.capital_endpoint(f"주요사항보고서({title})") == endpoint

    values = await dart_capital.enrich_capital_event(endpoint, FILING, event, {}, {})

    assert transport.call_args.args[0] == f"{endpoint}.json"
    by_metric = {
        value["metric_name"]: value["value_details"]["actual"] for value in values
    }
    assert by_metric[metric] == expected
    if endpoint == "tsstkAqDecsn":
        assert by_metric["buyback_shares_common"] == "10"
    if endpoint == "piicDecsn":
        assert "issue_price" not in by_metric and "payment_date" not in by_metric


@pytest.mark.asyncio
async def test_dividend_uses_labelled_date_and_company_fiscal_month(monkeypatch):
    document = BytesIO()
    with ZipFile(document, "w") as archive:
        archive.writestr(
            f"{RECEIPT}.xml",
            "<ROOT><TABLE>"
            "<TR><TD>배당금지급 예정일자</TD><TD>2026-11-30</TD></TR>"
            "<TR><TD>7. 배당기준일</TD><TD>2026년 9월 30일</TD></TR>"
            "</TABLE></ROOT>",
        )
    transport = Mock(
        side_effect=[document.getvalue(), b'{"status":"000","acc_mt":"09"}']
    )
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    event = {
        "source_event_id": RECEIPT,
        "title": "현금·현물배당 결정",
        "company_name": "테스트",
        "symbol": "005930",
        "raw_payload_json": {},
    }

    values = await dart_capital.enrich_capital_event("document", FILING, event, {}, {})

    assert dart_capital.capital_endpoint(event["title"]) == "document"
    assert [value["value_details"]["actual"] for value in values] == ["20260930", "9"]
    assert transport.call_args.args == ("company.json", {"corp_code": CORP})


@pytest.mark.asyncio
async def test_query_projection_preserves_precision_reason_and_legacy_bytes():
    legacy = MarketEventValue(metric_name="eps", actual=Decimal("1.25000000"))
    assert _value_response(legacy).model_dump_json() == (
        '{"metric_name":"eps","period":null,"actual":"1.25000000","forecast":null,'
        '"previous":null,"revised_previous":null,"unit":null,"surprise":null,'
        '"surprise_pct":null,"released_at":null}'
    )
    metric = MarketEventValue(
        metric_name="worst_case_dilution",
        unit="%",
        value_details={"actual": "0.12345678987654321"},
    )
    missing = MarketEventValue(
        metric_name="conversion_reset_floor",
        unit="KRW/share",
        value_details={"actual": None, "reason": "not_disclosed"},
    )
    assert (
        _value_response(metric).model_dump(mode="json")["actual"]
        == "0.12345678987654321"
    )
    assert _value_response(missing).model_dump(mode="json")["reason"] == "not_disclosed"

    result = MagicMock()
    result.all.return_value = [(RECEIPT, metric), (RECEIPT, missing)]
    query = MarketEventsQueryService(AsyncMock(execute=AsyncMock(return_value=result)))
    assert (await query.dart_values_by_receipt([RECEIPT]))[RECEIPT] == [
        {
            "metric_name": "worst_case_dilution",
            "unit": "%",
            "actual": "0.12345678987654321",
        },
        {
            "metric_name": "conversion_reset_floor",
            "unit": "KRW/share",
            "actual": None,
            "reason": "not_disclosed",
        },
    ]


@pytest.mark.asyncio
async def test_detail_error_keeps_base_and_later_filing(monkeypatch):
    transport = Mock(side_effect=[b'{"status":"800"}', response(CB)])
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    repo, db = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))
    later = {**FILING, "rcept_no": "20261124000002"}
    transport.side_effect = [
        b'{"status":"800"}',
        response(CB).replace(RECEIPT.encode(), later["rcept_no"].encode()),
    ]
    result = await ingestion.ingest_kr_disclosures_for_date(
        db,
        date(2026, 11, 24),
        fetch_rows=AsyncMock(return_value=[FILING, later]),
        enrich_capital=True,
    )
    assert result.status == "failed"
    assert result.event_count == 2
    db.rollback.assert_not_awaited()
    first, second = repo.upsert_event_with_values.await_args_list
    assert first.args[0]["source_event_id"] == RECEIPT and first.args[1] == []
    assert second.args[0]["source_event_id"] == later["rcept_no"] and second.args[1]
    repo.mark_partition_failed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registry_down", "expected"), [(False, "succeeded"), (True, "failed")]
)
async def test_unavailable_detail_preserves_existing_values(
    monkeypatch, registry_down, expected
):
    transport = Mock(return_value=b'{"status":"013"}')
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    row = dict(FILING)
    if registry_down:
        row.pop("corp_code")
    repo = AsyncMock()
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))
    result = await ingestion.ingest_kr_disclosures_for_date(
        AsyncMock(),
        date(2026, 11, 24),
        fetch_rows=AsyncMock(return_value=[row]),
        load_corp_registry=AsyncMock(return_value=None),
        enrich_capital=True,
    )
    assert result.status == expected
    call = repo.upsert_event_with_values.await_args
    assert call.args[1] == []
    assert not call.kwargs.get("replace_values", False)
    assert transport.call_count == int(not registry_down)


@pytest.mark.asyncio
async def test_document_xml_retries_020_and_treats_014_as_empty(monkeypatch):
    transport = Mock(
        side_effect=[
            b"<result><status>020</status></result>",
            b"<result><status>014</status></result>",
        ]
    )
    delay = AsyncMock()
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    monkeypatch.setattr(dart_capital.asyncio, "sleep", delay)
    assert await dart_capital._request("document.xml", rcept_no=RECEIPT) is None
    delay.assert_awaited_once_with(1)
    assert transport.call_count == 2


@pytest.mark.parametrize("relation", ["자회사", "종속회사"])
def test_subsidiary_dividend_is_not_attributed_to_parent(relation):
    assert (
        dart_capital.capital_endpoint(f"현금ㆍ현물배당결정 ({relation}의 주요경영사항)")
        is None
    )


def test_sentry_scrubs_opendart_query_and_named_credential():
    from app.monitoring.sentry import _before_breadcrumb, _before_send_transaction

    fake_key = "fixture-dart-key"
    query = f"corp_code={CORP}&crtfc_key={fake_key}"
    crumb = _before_breadcrumb(
        {"category": "httplib", "data": {"http.query": query}}, {}
    )
    transaction = _before_send_transaction(
        {"spans": [{"data": {"http.query": query, "crtfc_key": fake_key}}]}, {}
    )
    assert fake_key not in json.dumps([crumb, transaction])
    assert CORP in json.dumps([crumb, transaction])


def test_dart_migration_is_single_head_and_guards_existing_values():
    from io import StringIO

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    rev = script.get_revision("20261125_hk6582_dart_capital")
    assert rev is not None
    assert rev.down_revision == "20261125_hk6579_benzinga_pro"
    head = script.get_current_head()
    assert head is not None
    assert rev.revision in {
        migration.revision for migration in script.iterate_revisions(head, "base")
    }
    output = StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(ctx):
        rev.module.downgrade()
    sql = output.getvalue()
    assert sql.index("LOCK TABLE") < sql.index("value_details IS NOT NULL")
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP COLUMN value_details")


def test_dart_suite_is_in_exactly_one_ci_shard():
    from pathlib import Path

    name = "tests/services/market_events/test_dart_capital.py"
    assert (
        sum(
            manifest.read_text().splitlines().count(name)
            for manifest in Path("ci_shards").glob("shard-*.txt")
        )
        == 1
    )


def test_dart_migration_upgrade_accepts_a_metadata_created_column():
    from io import StringIO

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    rev = script.get_revision("20261125_hk6582_dart_capital")
    assert rev is not None
    output = StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(ctx):
        rev.module.upgrade()
    assert (
        "ALTER TABLE market_event_values ADD COLUMN IF NOT EXISTS value_details JSONB"
        in output.getvalue()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", ["empty_detail", "failed_detail", "paced_backfill"])
async def test_dart_list_replay_preserves_typed_event_evidence(monkeypatch, replay):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql.dml import Insert

    from app.services.market_events.repository import MarketEventsRepository

    insider = {
        **FILING,
        "rcept_no": "20261124000002",
        "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서",
    }
    insider_response = response(
        {
            "rcept_dt": "20261125",
            "sp_stock_lmp_rate": "7.10",
            "sp_stock_lmp_irds_rate": "-0.50",
        }
    ).replace(RECEIPT.encode(), insider["rcept_no"].encode())
    bodies = [response(CB), insider_response]
    if replay == "empty_detail":
        bodies += [b'{"status":"013"}', b'{"status":"013"}']
    elif replay == "failed_detail":
        bodies += [b'{"status":"800"}', b'{"status":"800"}']

    transport = Mock(side_effect=bodies)
    monkeypatch.setattr(dart_capital, "_get_bytes", transport)
    result = Mock()
    result.scalar_one.return_value = 1
    db = AsyncMock(execute=AsyncMock(return_value=result))
    repo = MarketEventsRepository(db)
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))
    monkeypatch.setattr(repo, "get_or_create_partition", AsyncMock())
    monkeypatch.setattr(repo, "mark_partition_running", AsyncMock())
    monkeypatch.setattr(repo, "mark_partition_succeeded", AsyncMock())
    monkeypatch.setattr(repo, "mark_partition_failed", AsyncMock())
    write_value = AsyncMock()
    monkeypatch.setattr(repo, "_upsert_value", write_value)
    fetch_rows = AsyncMock(return_value=[FILING, insider])

    def event_inserts():
        return [
            call.args[0].compile(dialect=postgresql.dialect())
            for call in db.execute.await_args_list
            if isinstance(call.args[0], Insert)
        ]

    first = await ingestion.ingest_kr_disclosures_for_date(
        db, date(2026, 11, 24), fetch_rows=fetch_rows, enrich_capital=True
    )
    assert first.status == "succeeded"
    bond_insert, insider_insert = event_inserts()
    assert "dart_reset_floor_basis" in bond_insert.params["raw_payload_json"]
    assert insider_insert.params["event_date"] == date(2026, 11, 25)
    assert write_value.await_count > 0

    db.execute.reset_mock()
    write_value.reset_mock()
    # The paced backfill calls ingestion with a custom fetcher and no enrichment option.
    kwargs = {} if replay == "paced_backfill" else {"enrich_capital": True}
    second = await ingestion.ingest_kr_disclosures_for_date(
        db, date(2026, 11, 24), fetch_rows=fetch_rows, **kwargs
    )
    assert second.status == ("failed" if replay == "failed_detail" else "succeeded")
    bond_replay, insider_replay = event_inserts()
    for sql in (bond_replay, insider_replay):
        assert "raw_payload_json" in sql.params  # Still insert when the receipt is new.
        conflict_update = str(sql).split("DO UPDATE SET ", 1)[1]
        assert "raw_payload_json =" not in conflict_update
        assert "event_date =" not in conflict_update
    write_value.assert_not_awaited()
    assert transport.call_count == len(bodies)


@pytest.mark.asyncio
async def test_ordinary_dart_replay_updates_date_payload_and_resolved_symbol(
    monkeypatch,
):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql.dml import Insert

    from app.services.market_events.repository import MarketEventsRepository

    result = Mock()
    result.scalar_one.return_value = 1
    db = AsyncMock(execute=AsyncMock(return_value=result))
    repo = MarketEventsRepository(db)
    monkeypatch.setattr(ingestion, "MarketEventsRepository", Mock(return_value=repo))
    monkeypatch.setattr(repo, "get_or_create_partition", AsyncMock())
    monkeypatch.setattr(repo, "mark_partition_running", AsyncMock())
    monkeypatch.setattr(repo, "mark_partition_succeeded", AsyncMock())
    initial = {**FILING, "report_nm": "기타공시", "stock_code": "", "rm": ""}
    updated = {**initial, "stock_code": "005930", "rcept_dt": "20261125", "rm": "정정"}
    registry = AsyncMock(return_value=None)

    for row in (initial, updated):
        outcome = await ingestion.ingest_kr_disclosures_for_date(
            db,
            date(2026, 11, 24),
            fetch_rows=AsyncMock(return_value=[row]),
            load_corp_registry=registry,
        )
        assert outcome.status == "succeeded"

    first, second = [
        call.args[0].compile(dialect=postgresql.dialect())
        for call in db.execute.await_args_list
        if isinstance(call.args[0], Insert)
    ]
    assert (
        first.params["raw_payload_json"]["symbol_resolution_reason"]
        == "registry_unavailable"
    )
    update = str(second).split("DO UPDATE SET ", 1)[1]
    assert "event_date =" in update
    assert "symbol =" in update
    assert "raw_payload_json =" in update
    assert date(2026, 11, 25) in second.params.values()
    assert {"정정"} == {
        value["rm"] for value in second.params.values() if isinstance(value, dict)
    }
    assert all(
        "symbol_resolution_reason" not in value
        for value in second.params.values()
        if isinstance(value, dict)
    )
