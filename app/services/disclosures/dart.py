from __future__ import annotations

import asyncio
import datetime as dt

import dart_fss

from app.core.config import settings
from data.disclosures.dart_corp_index import NAME_TO_CORP, prime_index


async def list_filings(korean_name: str, days: int = 3):
    """Query DART API for corporation filings.

    Args:
        korean_name: Korean company name (e.g., "삼성전자")
        days: Number of days to look back (default: 3, max: 365)

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
        reports = corp.search_filings(bgn_de=bgn, end_de=end, page_count=100)
        return [(r.rcept_dt, r.report_nm, r.rcept_no, korean_name) for r in reports]

    try:
        results = await asyncio.to_thread(fetch_sync)
    except Exception as exc:
        if not settings.opendart_api_key:
            return {
                "success": False,
                "error": "OPENDART_API_KEY not set. Please set environment variable.",
                "filings": [],
            }
        return {
            "success": False,
            "error": str(exc),
            "filings": [],
        }

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
