from __future__ import annotations

import asyncio
import datetime as dt
import enum

import dart_fss

from app.core.config import settings
from data.disclosures.dart_corp_index import NAME_TO_CORP, prime_index


class ReportType(str, enum.Enum):
    """DART 공시 유형 (Report Type).

    - periodic: 정기 (반기/사업/분기보고서)
    - major_events: 주요사항 (주요사항보고)
    - issuance: 발행 (증권발행실적보고)
    - shareholding: 지분 (지분공시)
    - other: 기타 (기타공시)
    """

    periodic = "A"
    major_events = "B"
    issuance = "C"
    shareholding = "D"
    other = "E"


KOREAN_REPORT_TYPE_MAP: dict[str, ReportType] = {
    "정기": ReportType.periodic,
    "주요사항": ReportType.major_events,
    "발행": ReportType.issuance,
    "지분": ReportType.shareholding,
    "기타": ReportType.other,
}


async def list_filings(
    korean_name: str,
    days: int = 3,
    limit: int = 20,
    report_type: str | None = None,
) -> list[dict] | dict:
    """Query DART API for corporation filings.

    Args:
        korean_name: Korean company name (e.g., "삼성전자")
        days: Number of days to look back (default: 3, max: 365)
        limit: Maximum number of filings to return (default: 20, max: 100)
        report_type: Filter by report type ("정기", "주요사항", "발행", "지분", "기타")

    Returns:
        List of filing records with date, report name, report number, corp name.
    """
    if not NAME_TO_CORP:
        await prime_index()

    corp_code = NAME_TO_CORP.get(korean_name)
    if not corp_code:
        return []

    bgn = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")

    def fetch_sync():
        dart_fss.set_api_key(settings.opendart_api_key)
        corp = dart_fss.corp.Corp(corp_code=corp_code)

        pblntf_ty = None
        if report_type:
            report_type_enum = KOREAN_REPORT_TYPE_MAP.get(report_type.strip())
            if report_type_enum:
                pblntf_ty = report_type_enum.value

        reports = corp.search_filings(
            bgn_de=bgn,
            end_de=end,
            pblntf_ty=pblntf_ty,
            page_count=min(limit, 100),
        )
        return [
            (r.rcept_dt, r.report_nm, r.rcept_no, korean_name) for r in reports[:limit]
        ]

    if not settings.opendart_api_key:
        return {
            "success": False,
            "error": "OPENDART_API_KEY not set. Please set environment variable.",
            "filings": [],
        }

    try:
        results = await asyncio.to_thread(fetch_sync)
    except Exception as exc:
        return {"success": False, "error": str(exc), "filings": []}

    filings = []
    for filing_date, report_nm, rcept_no, korean_name in results:
        filings.append(
            {
                "date": f"{filing_date[:4]}-{filing_date[4:6]}-{filing_date[6:8]}",
                "report_nm": report_nm,
                "rcp_no": rcept_no,
                "corp_name": korean_name,
            }
        )

    return filings
