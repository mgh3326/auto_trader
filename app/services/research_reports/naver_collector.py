"""Collect Naver research metadata and pass it through the existing ingest service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import uuid4

from app.schemas.research_reports import ResearchReportIngestionRequest
from app.services.research_reports.ingestion import ingest_research_reports_v1
from app.services.research_reports.naver_client import CATEGORIES, NaverResearchClient, strip_html
from app.services.research_reports.repository import ResearchReportsRepository

PAGE_SIZE = 20
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class KnownReportRepository(Protocol):
    async def known_source_ids(self, *, source: str) -> set[str]: ...


@dataclass(frozen=True)
class CollectionResult:
    reports: list[dict[str, Any]]
    category_errors: dict[str, str]
    category_counts: dict[str, int]


def _decimal_text(value: Any) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, "not_provided"
    if type(value) not in (str, int):
        raise ValueError("invalid_goal_price")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid_goal_price") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("invalid_goal_price")
    return format(number, "f"), None


def map_report(category: str, item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    nid = item["nid"]
    date_text = item.get("writeDate")
    if date_text is not None and (type(date_text) is not str or not _DATE_RE.fullmatch(date_text)):
        raise ValueError("invalid_write_date")
    if date_text:
        # Validate the calendar date while preserving date-only precision.
        datetime.strptime(date_text, "%Y-%m-%d")

    target_price, target_price_reason = _decimal_text(
        detail.get("goalPrice", item.get("goalPrice"))
    )
    content = item.get("content")
    if content is not None and type(content) is not str:
        raise ValueError("invalid_content")
    summary = strip_html(content or "")[:1000]
    excerpt = summary[:500]
    broker = item.get("brokerName")
    broker_code = item.get("brokerCode")
    opinion = detail.get("opinionText", item.get("opinionText"))
    opinion_type = detail.get("opinionType", item.get("opinionType"))
    item_code = item.get("itemCode")
    item_name = item.get("itemName")
    symbols: list[dict[str, Any]] = []
    if item_code:
        if type(item_code) is not str or not re.fullmatch(r"\d{6}", item_code):
            raise ValueError("invalid_item_code")
        symbols.append(
            {
                "symbol": item_code,
                "market": "kr",
                "source": "naver_research",
                "name": item_name,
                "broker_code": broker_code,
                "goal_price": target_price,
                "goal_price_reason": target_price_reason,
                "opinion": opinion,
                "opinion_type": opinion_type,
            }
        )
    note_parts = [f"opinion={opinion}" if opinion else None,
                  f"opinion_type={opinion_type}" if opinion_type else None,
                  f"goal_price={target_price}" if target_price is not None else None,
                  f"goal_price_reason={target_price_reason}" if target_price_reason else None,
                  f"item_name={item_name}" if item_name and not item_code else None]
    note = "; ".join(part for part in note_parts if part) or None
    pdf_url = detail.get("attachUrl")
    pdf_name = detail.get("attachName")
    if pdf_url is not None and (type(pdf_url) is not str or not pdf_url.startswith("https://")):
        raise ValueError("invalid_pdf_reference")
    if pdf_name is not None and type(pdf_name) is not str:
        raise ValueError("invalid_pdf_name")
    return {
        "dedup_key": f"naver_research:{nid}",
        "report_type": "equity_research",
        "source": "naver_research",
        "source_report_id": nid,
        "title": item.get("title"),
        "category": category,
        "analyst": broker,
        "published_at_text": date_text,
        "published_at": None,
        "summary_text": summary,
        "detail": {"title": opinion, "subtitle": note, "excerpt": excerpt},
        "pdf": {"url": pdf_url, "filename": pdf_name},
        "symbol_candidates": symbols,
        "raw_text_policy": "metadata_only; naver content truncated to summary/excerpt limits",
        "attribution": {
            "publisher": broker,
            "copyright_notice": "Naver research report; metadata and short excerpt only",
            "full_text_exported": False,
            "pdf_body_exported": False,
        },
    }


async def collect_naver_reports(
    client: NaverResearchClient,
    repository: KnownReportRepository,
    *,
    categories: tuple[str, ...] = CATEGORIES,
    size: int = PAGE_SIZE,
) -> CollectionResult:
    known = await repository.known_source_ids(source="naver_research")
    reports: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    counts: dict[str, int] = {}
    for category in categories:
        count = 0
        index = 0
        try:
            while True:
                page = await client.list_reports(category, size=size, index=index)
                items = page["items"]
                if not items:
                    if page["hasNext"]:
                        raise ValueError("empty_page_with_has_next")
                    break
                reached_known = False
                for item in items:
                    if item["nid"] in known:
                        reached_known = True
                        break
                    detail = await client.detail(category, item["nid"])
                    reports.append(map_report(category, item, detail))
                    known.add(item["nid"])
                    count += 1
                if reached_known or not page["hasNext"]:
                    break
                index += len(items)
        except Exception as exc:
            errors[category] = str(exc)[:160] or type(exc).__name__
        counts[category] = count
    return CollectionResult(reports=reports, category_errors=errors, category_counts=counts)


async def run_naver_research_ingestion(
    db: Any,
    client: NaverResearchClient,
    *,
    categories: tuple[str, ...] = CATEGORIES,
    size: int = PAGE_SIZE,
    now: datetime | None = None,
) -> tuple[CollectionResult, Any | None]:
    repo = ResearchReportsRepository(db)
    result = await collect_naver_reports(client, repo, categories=categories, size=size)
    started_at = now or datetime.now(UTC)
    request = ResearchReportIngestionRequest.model_validate(
        {
            "research_report_ingestion_run": {
                "run_uuid": f"naver-{uuid4()}",
                "payload_version": "research-reports.v1",
                "source": "naver_research",
                "started_at": started_at,
                "report_count": len(result.reports),
                "errors": result.category_errors or None,
                "flags": {"category_counts": result.category_counts},
                "copyright_notice": "Naver research metadata only; no full text or PDF body",
            },
            "reports": result.reports,
        }
    )
    response = await ingest_research_reports_v1(db, request)
    return result, response
