"""Naver Finance crawling service for Korean equities.

This package provides async functions to fetch:
- Stock news and disclosures
- Company profile information
- Financial statements
- Foreign/institutional investor trends
- Securities firm investment opinions
- Valuation metrics and sector peer analysis
"""

from app.services.naver_finance.company import (
    fetch_company_profile as fetch_company_profile,
    fetch_financials as fetch_financials,
)
from app.services.naver_finance.investor import (
    _build_investment_opinions_from_company_list_soup as _build_investment_opinions_from_company_list_soup,
    _collect_opinion_report_infos as _collect_opinion_report_infos,
    _fetch_current_price as _fetch_current_price,
    _fetch_kr_snapshot as _fetch_kr_snapshot,
    _fetch_report_detail as _fetch_report_detail,
    _fetch_report_detail_with_client as _fetch_report_detail_with_client,
    _parse_report_detail_soup as _parse_report_detail_soup,
    fetch_investment_opinions as fetch_investment_opinions,
    fetch_investor_trends as fetch_investor_trends,
)
from app.services.naver_finance.news import (
    _parse_news_soup as _parse_news_soup,
    fetch_news as fetch_news,
)
from app.services.naver_finance.parser import (
    DEFAULT_HEADERS as DEFAULT_HEADERS,
    NAVER_FINANCE_BASE as NAVER_FINANCE_BASE,
    NAVER_FINANCE_ITEM as NAVER_FINANCE_ITEM,
    _decode_html_content as _decode_html_content,
    _extract_current_price_from_main_soup as _extract_current_price_from_main_soup,
    _fetch_html as _fetch_html,
    _fetch_html_with_client as _fetch_html_with_client,
    _parse_naver_date as _parse_naver_date,
)
from app.services.naver_finance.valuation import (
    NAVER_MOBILE_API as NAVER_MOBILE_API,
    _fetch_integration as _fetch_integration,
    _fetch_sector_name as _fetch_sector_name,
    _fetch_sector_stock_codes as _fetch_sector_stock_codes,
    _parse_total_infos as _parse_total_infos,
    _parse_valuation_from_soups as _parse_valuation_from_soups,
    fetch_sector_peers as fetch_sector_peers,
    fetch_valuation as fetch_valuation,
)

__all__ = [
    "DEFAULT_HEADERS",
    "NAVER_FINANCE_BASE",
    "NAVER_FINANCE_ITEM",
    "NAVER_MOBILE_API",
    "_build_investment_opinions_from_company_list_soup",
    "_collect_opinion_report_infos",
    "_decode_html_content",
    "_extract_current_price_from_main_soup",
    "_fetch_current_price",
    "_fetch_html",
    "_fetch_html_with_client",
    "_fetch_integration",
    "_fetch_kr_snapshot",
    "_fetch_report_detail",
    "_fetch_report_detail_with_client",
    "_fetch_sector_name",
    "_fetch_sector_stock_codes",
    "_parse_naver_date",
    "_parse_news_soup",
    "_parse_report_detail_soup",
    "_parse_total_infos",
    "_parse_valuation_from_soups",
    "fetch_company_profile",
    "fetch_financials",
    "fetch_investment_opinions",
    "fetch_investor_trends",
    "fetch_news",
    "fetch_sector_peers",
    "fetch_valuation",
]
