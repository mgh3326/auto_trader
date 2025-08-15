from __future__ import annotations
import datetime as dt
import dart_fss  # 또는 httpx로 OpenDART 직접 호출
from app.core.config import settings  # OPEN_DART_API_KEY
from data.disclosures.dart_corp_index import NAME_TO_CORP, prime_index


async def init_dart():
    # 최초 한 번 인덱스 준비
    await prime_index()
    dart_fss.set_api_key(api_key=settings.opendart_api_key)


async def list_filings(korean_name: str, days: int = 3) -> list[dict]:
    # 회사명 -> corp_code
    corp_code = NAME_TO_CORP.get(korean_name)
    if not corp_code:
        return []
    bgn = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    # dart-fss 사용 예시 (간단화)
    corp = dart_fss.corp.Corp(corp_code=corp_code)

    # 3A) 권장: Corp 메서드 사용
    reports = corp.search_filings(bgn_de=bgn, end_de=end, page_count=100)

    # 3B) 또는 탑레벨 함수 사용 (동일 결과)
    # from dart_fss.filings import search as search_filings
    # reports = search_filings(corp_code=corp.corp_code, bgn_de=bgn, end_de=end, page_count=100)
    out = []
    for r in reports:
        out.append(
            {
                "date": r.rcp_dt,
                "report_nm": r.report_nm,
                "rcp_no": r.rcp_no,
                "corp_name": korean_name,
            }
        )
    return out
    # 4) 결과 다루기
    # SearchResults는 인덱싱/슬라이싱 가능, dict로도 변환 가능
    items = reports.to_dict().get("list", [])
    for r in items:
        print(r["rcp_dt"], r["report_nm"], r["rcp_no"])
