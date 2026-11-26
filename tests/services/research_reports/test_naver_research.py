from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).parents[2] / "fixtures" / "naver_research" / "2026-11-26"


def _captured_item(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "nid": record["nid"],
        "title": record["title"],
        "content": record["summary"],
        "brokerName": record["broker"],
        "brokerCode": record["broker_code"],
        "writeDate": record["write_date"],
        "itemCode": record["item_code"],
        "itemName": record["item_name"],
        "goalPrice": record["goal_price"],
        "opinionText": record["opinion"],
        "opinionType": record["opinion_type"],
    }


@pytest.mark.unit
def test_each_category_capture_maps_to_copyright_safe_report_metadata():
    from app.schemas.research_reports import ResearchReportPayloadV1
    from app.services.research_reports.naver_client import CATEGORIES
    from app.services.research_reports.naver_collector import map_report

    for category in CATEGORIES:
        captured = json.loads(
            (FIXTURES / f"{category}-0.json").read_text(encoding="utf-8")
        )
        record = captured["at_records"][0]
        item = _captured_item(record)
        detail = {"nid": item["nid"], "attachUrl": None, "attachName": None}
        report = ResearchReportPayloadV1.model_validate(
            map_report(category, item, detail)
        )
        assert report.source_report_id == item["nid"]
        assert report.category == category
        assert report.published_at_text == item["writeDate"]
        assert report.published_at is None  # a calendar date does not imply a time
        assert len(report.summary_text or "") <= 1000
        assert len(report.detail.excerpt or "") <= 500
        assert report.attribution.full_text_exported is False
        assert report.attribution.pdf_body_exported is False
        assert not hasattr(report, "pdf_body")

    company = json.loads((FIXTURES / "company-0.json").read_text(encoding="utf-8"))
    priced = next(row for row in company["at_records"] if row["goal_price"])
    mapped = ResearchReportPayloadV1.model_validate(
        map_report("company", _captured_item(priced), {"nid": priced["nid"]})
    )
    assert mapped.symbol_candidates[0].goal_price == "150000"


@pytest.mark.unit
def test_company_detail_capture_html_and_pdf_are_only_short_text_and_reference():
    from app.schemas.research_reports import ResearchReportPayloadV1
    from app.services.research_reports.naver_collector import map_report

    saved = json.loads(
        (FIXTURES / "detail-company-98500.json").read_text(encoding="utf-8")
    )
    detail = saved["researchContent"]
    item = {
        "nid": detail["nid"],
        "title": detail["title"],
        "content": detail["content"] + " " + "<p>추가 문장</p>" * 400,
        "brokerName": detail["brokerName"],
        "brokerCode": detail["brokerCode"],
        "writeDate": detail["writeDate"],
        "itemCode": "142210",
        "itemName": "유니트론텍",
        "goalPrice": detail.get("goalPrice"),
        "opinionText": detail.get("opinionText"),
        "opinionType": detail.get("opinionType"),
    }
    detail["attachUrl"] = "https://stock.pstatic.net/research/company/98500.pdf"
    detail["attachName"] = "98500.pdf"
    report = ResearchReportPayloadV1.model_validate(map_report("company", item, detail))
    assert "<p>" not in (report.summary_text or "")
    assert len(report.summary_text or "") == 1000
    assert len(report.detail.excerpt or "") == 500
    assert report.pdf.url == detail["attachUrl"]
    assert report.pdf.filename == "98500.pdf"


@pytest.mark.unit
def test_naver_client_reads_saved_by_items_capture_with_strict_shape():
    from app.services.research_reports.naver_client import parse_by_items_response

    captured = json.loads((FIXTURES / "by-items.json").read_text(encoding="utf-8"))
    result = parse_by_items_response(captured, item_codes=("005930",))
    assert len(result["005930"]) == len(captured["005930"])
    assert all(row["nid"].isdecimal() for row in result["005930"])


class _FakeRepository:
    def __init__(self, known: set[str] | None = None):
        self.known = known or set()

    async def known_source_ids(self, *, source: str) -> set[str]:
        assert source == "naver_research"
        return set(self.known)


class _FakeClient:
    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.list_calls: list[tuple[str, int]] = []
        self.detail_calls: list[tuple[str, str]] = []

    async def aclose(self):
        return None

    async def list_reports(self, category: str, *, size: int, index: int):
        self.list_calls.append((category, index))
        if category in self.fail:
            raise RuntimeError("fixture category failure")
        if index == 0:
            return {
                "hasNext": True,
                "index": 0,
                "items": [
                    {
                        "nid": "200",
                        "title": "new",
                        "content": "<p>짧은 요약</p>",
                        "brokerName": "증권사",
                        "brokerCode": "01",
                        "writeDate": "2026-11-26",
                        "itemCode": None,
                        "itemName": None,
                        "goalPrice": None,
                        "opinionText": None,
                        "opinionType": None,
                    },
                    {
                        "nid": "100",
                        "title": "old",
                        "content": "",
                        "brokerName": "증권사",
                        "brokerCode": "01",
                        "writeDate": "2026-11-25",
                        "itemCode": None,
                        "itemName": None,
                        "goalPrice": None,
                        "opinionText": None,
                        "opinionType": None,
                    },
                ],
            }
        return {"hasNext": False, "index": index, "items": []}

    async def detail(self, category: str, nid: str):
        self.detail_calls.append((category, nid))
        return {"nid": nid, "attachUrl": None, "attachName": None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_stops_at_known_nid_without_fetching_or_returning_it():
    from app.services.research_reports.naver_collector import collect_naver_reports

    client = _FakeClient()
    result = await collect_naver_reports(
        client, _FakeRepository({"100"}), categories=("company",), size=2
    )
    assert [row["source_report_id"] for row in result.reports] == ["200"]
    assert client.list_calls == [("company", 0)]
    assert client.detail_calls == [("company", "200")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_reports_failed_category_and_keeps_other_category_rows():
    from app.services.research_reports.naver_collector import collect_naver_reports

    client = _FakeClient(fail={"industry"})
    result = await collect_naver_reports(
        client, _FakeRepository({"100"}), categories=("industry", "company"), size=2
    )
    assert "industry" in result.category_errors
    assert [row["source_report_id"] for row in result.reports] == ["200"]
    assert result.category_counts == {"industry": 0, "company": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_category_failure_still_sends_successful_rows_to_ingestion(
    monkeypatch,
):
    import app.services.research_reports.naver_collector as collector

    observed: dict[str, Any] = {}

    class Repository(_FakeRepository):
        def __init__(self, db):
            super().__init__({"100"})

    async def fake_ingest(db, request):
        observed["categories"] = {row.category for row in request.reports}
        observed["errors"] = request.research_report_ingestion_run.errors
        return object()

    monkeypatch.setattr(collector, "ResearchReportsRepository", Repository)
    monkeypatch.setattr(collector, "ingest_research_reports_v1", fake_ingest)
    result, response = await collector.run_naver_research_ingestion(
        object(),
        _FakeClient(fail={"industry"}),
        categories=("industry", "company"),
        size=2,
    )
    assert response is not None
    assert result.category_errors == {"industry": "fixture category failure"}
    assert observed["categories"] == {"company"}
    assert observed["errors"] == result.category_errors


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_client_sends_user_agent_refuses_redirects_and_paces_requests():
    from app.services.research_reports.naver_client import (
        USER_AGENT,
        NaverResearchClient,
        NaverResearchError,
    )

    now = [0.0]
    sleeps: list[float] = []
    seen_headers: list[str | None] = []
    statuses = [200, 302]

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("user-agent"))
        status = statuses.pop(0)
        if status == 302:
            return httpx.Response(status, headers={"location": "https://example.test/"})
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "hasNext": False,
                "totalCount": "0",
                "items": [],
                "size": "20",
                "index": "0",
            },
        )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    client = NaverResearchClient(http, sleep=sleep, monotonic=lambda: now[0])
    assert (await client.list_reports("company"))["items"] == []
    with pytest.raises(NaverResearchError, match="redirect_refused"):
        await client.list_reports("company")
    assert seen_headers == [USER_AGENT, USER_AGENT]
    assert sleeps == [0.5]
    await http.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cli_dry_run_collects_metadata_without_opening_database(
    monkeypatch, capsys
):
    import app.services.research_reports.naver_collector as collector
    import scripts.ingest_naver_research as cli

    client = _FakeClient()

    class Client:
        def __new__(cls):
            return client

    async def no_database_collection(got_client, repository):
        assert got_client is client
        assert isinstance(repository, cli._EmptyRepository)
        from app.services.research_reports.naver_collector import CollectionResult

        return CollectionResult(
            reports=[], category_errors={}, category_counts={"company": 0}
        )

    monkeypatch.setattr(cli, "NaverResearchClient", Client)
    monkeypatch.setattr(collector, "collect_naver_reports", no_database_collection)
    assert await cli.main_async(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


@pytest.mark.unit
def test_decimal_conversion_preserves_source_precision_and_absence_reason():
    from app.services.research_reports.naver_collector import _decimal_text

    value, reason = _decimal_text("123450.6700")
    assert Decimal(value) == Decimal("123450.6700")
    assert value == "123450.6700"
    assert reason is None
    assert _decimal_text(None) == (None, "not_provided")
