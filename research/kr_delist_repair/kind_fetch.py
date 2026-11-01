"""Paced read-only fetcher for public KIND pages and KIS master files.

Every response body is saved under ``raw/`` with its SHA-256 in
``raw/index.json`` so the offline ``build`` step can be rerun and audited
without network access.  No credentials are used; no retries are made.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

KIND = "https://kind.krx.co.kr"
KIS_MASTER = "https://new.real.download.dws.co.kr/common/master"
USER_AGENT = "Mozilla/5.0 (research; ROB-1430 kr-delist-repair)"
MIN_INTERVAL_SEC = 1.5
KIND_MARKET_CODES = {"KOSPI": "1", "KOSDAQ": "2"}
KIS_MASTER_FILES = {
    "KOSPI": ("kospi_code.mst.zip", 228),
    "KOSDAQ": ("kosdaq_code.mst.zip", 222),
}
MARKET_DIVISIONS = ("유가증권시장본부", "코스닥시장본부")

_DETAIL_FORM = {
    "method": "searchDetailsSub",
    "forward": "details_sub",
    "currentPageSize": "100",
    "pageIndex": "1",
    "orderMode": "1",
    "orderStat": "D",
    "searchCodeType": "number",
    "allRepIsuSrtCd": "",
    "oldSearchCorpName": "",
    "reportCd": "",
    "business": "",
    "marketType": "",
    "settlementMonth": "",
    "securities": "",
    "submitOblgNm": "",
    "enterprise": "",
    "bfrDsclsType": "on",
    "reportNm": "상장폐지",
    "reportNmTemp": "상장폐지",
}


class RawStore:
    """Content-addressed record of every fetched public page."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"
        self.index: dict[str, dict[str, str]] = (
            json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        )

    def has(self, name: str) -> bool:
        return name in self.index and (self.root / name).exists()

    def read(self, name: str) -> bytes:
        payload = (self.root / name).read_bytes()
        expected = self.index[name]["sha256"]
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"raw file {name} does not match its recorded sha256")
        return payload

    def put(self, name: str, payload: bytes, url: str, params: dict[str, str]) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        self.index[name] = {
            "url": url,
            "params": json.dumps(params, ensure_ascii=False, sort_keys=True),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.index_path.write_text(
            json.dumps(self.index, ensure_ascii=False, indent=1, sort_keys=True)
        )


class KindFetcher:
    def __init__(self, store: RawStore) -> None:
        self.store = store
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=False,
        )
        self._last = 0.0
        self.requests = 0
        self._session_ready = False

    def close(self) -> None:
        self.client.close()

    def _pace(self) -> None:
        wait = MIN_INTERVAL_SEC - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1

    def _fetch(
        self, name: str, method: str, url: str, params: dict[str, str], **kw: object
    ) -> bytes:
        if self.store.has(name):
            return self.store.read(name)
        self._pace()
        if method == "GET":
            response = self.client.get(url, params=params, **kw)  # type: ignore[arg-type]
        else:
            response = self.client.post(url, data=params, **kw)  # type: ignore[arg-type]
        response.raise_for_status()
        self.store.put(name, response.content, url, params)
        return response.content

    def _ensure_session(self) -> None:
        if not self._session_ready:
            self._pace()
            self.client.get(
                f"{KIND}/disclosure/details.do", params={"method": "searchDetailsMain"}
            )
            self._session_ready = True

    def kis_master(self, market: str) -> bytes:
        zip_name, _ = KIS_MASTER_FILES[market]
        return self._fetch(
            f"kis_master/{zip_name}", "GET", f"{KIS_MASTER}/{zip_name}", {}
        )

    def delist_list(self, market: str, from_date: date, to_date: date) -> bytes:
        params = {
            "method": "searchDelCompanySub",
            "forward": "delcompany_down",
            "currentPageSize": "3000",
            "pageIndex": "1",
            "orderMode": "2",
            "orderStat": "D",
            "searchCorpName": "",
            "marketType": KIND_MARKET_CODES[market],
            "fromDate": from_date.isoformat(),
            "toDate": to_date.isoformat(),
        }
        name = f"kind_delist/{market}_{from_date:%Y%m%d}_{to_date:%Y%m%d}.xls"
        return self._fetch(name, "POST", f"{KIND}/investwarn/delcompany.do", params)

    def disclosure_search(
        self, company_code: str, from_date: date, to_date: date
    ) -> bytes:
        if (to_date - from_date) > timedelta(days=365 * 3):
            raise ValueError("KIND detail search accepts at most a three-year window")
        name = f"kind_search/{company_code}_{from_date:%Y%m%d}_{to_date:%Y%m%d}.html"
        if self.store.has(name):
            return self.store.read(name)
        self._ensure_session()
        params = dict(_DETAIL_FORM)
        params.update(
            {
                "repIsuSrtCd": f"A{company_code}",
                "searchCorpName": company_code,
                "fromDate": from_date.isoformat(),
                "toDate": to_date.isoformat(),
            }
        )
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{KIND}/disclosure/details.do?method=searchDetailsMain",
        }
        payload = self._fetch(
            name, "POST", f"{KIND}/disclosure/details.do", params, headers=headers
        )
        if "페이지 오류" in payload.decode("utf-8", "replace"):
            raise RuntimeError(f"KIND rejected the detail search for {company_code}")
        return payload

    def disclosure_viewer(self, acptno: str) -> bytes:
        return self._fetch(
            f"kind_viewer/{acptno}.html",
            "GET",
            f"{KIND}/common/disclsviewer.do",
            {
                "method": "search",
                "acptno": acptno,
                "docno": "",
                "viewerhost": "",
                "viewerport": "",
            },
        )

    def disclosure_contents(self, docno: str) -> bytes:
        return self._fetch(
            f"kind_contents/{docno}.html",
            "GET",
            f"{KIND}/common/disclsviewer.do",
            {"method": "searchContents", "docNo": docno},
        )

    def disclosure_body(self, docno: str, url: str) -> bytes:
        if not url.startswith(f"{KIND}/external/"):
            raise ValueError(f"refusing non-KIND body URL {url}")
        return self._fetch(f"kind_body/{docno}.htm", "GET", url, {})


def decode_kind(payload: bytes) -> str:
    for encoding in ("utf-8", "cp949"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("KIND page is neither UTF-8 nor CP949")
