"""Budgeted, resumable OpenDART collector; transport is injectable."""

from __future__ import annotations

import calendar
import hashlib
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


class BudgetExhausted(Exception):
    pass


class SourceError(Exception):
    pass


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(endpoint, params, key):
    query = urllib.parse.urlencode({**params, "crtfc_key": key})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            "https://opendart.fss.or.kr/api/" + endpoint + "?" + query, timeout=30
        ) as response:
            body = response.read()
    except Exception:
        body = None
    # Raise outside the except block so __context__ stays None; the original
    # transport exception can carry the full request URL, key included.
    if body is None:
        raise SourceError("OpenDART request failed")
    return body


class Collector:
    def __init__(self, root, run_id, start, end, key, budget, transport=http_get):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("invalid run ID")
        self.start, self.end = date.fromisoformat(start), date.fromisoformat(end)
        if (
            self.start < date(2015, 1, 1)
            or self.end < self.start
            or self.end >= datetime.now(ZoneInfo("Asia/Seoul")).date()
        ):
            raise ValueError("invalid filing coverage window")
        if not key or budget < 0 or budget > 20000:
            raise ValueError("invalid key or budget")
        self.path = Path(root) / "cache" / run_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.key, self.budget, self.used, self.transport = key, budget, 0, transport
        identity = {"start": start, "end": end}
        identity_path = self.path / "identity.json"
        if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
            raise ValueError("run ID already belongs to another coverage window")
        if not identity_path.exists():
            tmp = identity_path.with_name(identity_path.name + ".tmp")
            tmp.write_bytes(_json_bytes(identity))
            os.replace(tmp, identity_path)

    def get(self, endpoint, params, binary=False):
        cache_key = hashlib.sha256(_json_bytes([endpoint, params])).hexdigest()
        path = self.path / (cache_key + ".cache")
        if path.exists():
            return path.read_bytes() if binary else json.loads(path.read_bytes())
        if self.used >= self.budget:
            raise BudgetExhausted
        self.used += 1
        try:
            body = self.transport(endpoint, params, self.key)
        except Exception:
            body = None
        # Raised outside except so __context__ is None (ROB-1388 pattern): the
        # transport exception may embed the key-bearing request URL.
        if body is None:
            raise SourceError("OpenDART request failed")
        if self.key.encode() in body:
            raise SourceError("OpenDART response rejected")
        if binary:
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    if any(
                        self.key.encode() in archive.read(name)
                        for name in archive.namelist()
                    ):
                        raise SourceError("OpenDART response rejected")
            except zipfile.BadZipFile:
                raise SourceError("Invalid OpenDART response") from None
        if not binary:
            try:
                payload = json.loads(body)
                if payload.get("status") not in ("000", "013"):
                    raise SourceError("OpenDART returned an error status")
            except (ValueError, AttributeError):
                raise SourceError("Invalid OpenDART response") from None
        # Write via a temp name so a kill mid-write cannot leave a truncated
        # file that a resume later treats as a valid cache hit.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)
        return body if binary else payload

    def collect(self):
        try:
            return self._collect()
        except BudgetExhausted:
            return {"complete": False, "budget_used": self.used}

    def _collect(self):
        archive = self.get("corpCode.xml", {}, binary=True)
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                names = z.namelist()
                if len(names) != 1:
                    raise ValueError
                xml = ElementTree.fromstring(z.read(names[0]))
            mapping = {
                x.findtext("corp_code"): x.findtext("stock_code", "").strip()
                for x in xml.findall("list")
                if x.findtext("stock_code", "").strip()
            }
        except (ValueError, zipfile.BadZipFile, ElementTree.ParseError):
            raise SourceError("Invalid corporation-code response") from None
        filings = []
        current = self.start
        while current <= self.end:
            last = min(
                self.end,
                date(
                    current.year,
                    current.month,
                    calendar.monthrange(current.year, current.month)[1],
                ),
            )
            page = 1
            while True:
                response = self.get(
                    "list.json",
                    {
                        "bgn_de": current.strftime("%Y%m%d"),
                        "end_de": last.strftime("%Y%m%d"),
                        "pblntf_ty": "A",
                        "last_reprt_at": "N",
                        "page_no": str(page),
                        "page_count": "100",
                    },
                )
                if response["status"] == "000" and (
                    not isinstance(response.get("list"), list)
                    or "total_page" not in response
                ):
                    raise SourceError("Invalid disclosure response")
                filings.extend(response.get("list", []))
                if page >= int(response.get("total_page", 1)):
                    break
                page += 1
            current = last.fromordinal(last.toordinal() + 1)
        groups = {}
        missing = []
        for item in filings:
            name = item.get("report_nm", "")
            match = re.search(
                r"(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)", name
            )
            corp = item.get("corp_code")
            if not match:
                continue
            symbol = mapping.get(corp) or (item.get("stock_code") or "").strip()
            if not symbol:
                missing.append(
                    {
                        "corp_code": corp,
                        "rcept_no": item.get("rcept_no"),
                        "reason": "symbol_unavailable",
                    }
                )
                continue
            mapping[corp] = symbol
            kind, year, month = match.groups()
            code = {"사업보고서": "11011", "반기보고서": "11012"}.get(kind)
            if kind == "분기보고서":
                code = {"03": "11013", "09": "11014"}.get(month)
            if not code:
                missing.append(
                    {
                        "corp_code": corp,
                        "rcept_no": item.get("rcept_no"),
                        "reason": "unsupported_fiscal_period",
                    }
                )
                continue
            group_key = (corp, year, code, month)
            groups.setdefault(group_key, []).append(
                {
                    "rcept_no": item["rcept_no"],
                    "rcept_date": item["rcept_dt"],
                    "amendment": "정정" in name,
                }
            )
        reports = []
        for (corp, year, code, month), history in sorted(groups.items()):
            history.sort(key=lambda x: (x["rcept_date"], x["rcept_no"]))
            latest = history[-1]
            base = {
                "symbol": mapping[corp],
                "corp_code": corp,
                "business_year": year,
                "report_code": code,
                "period": f"{year}-{month}",
                "history": history,
                "rcept_no": latest["rcept_no"],
                "rcept_date": latest["rcept_date"],
                "amendment": latest["amendment"],
            }
            params = {"corp_code": corp, "bsns_year": year, "reprt_code": code}
            for basis in ("CFS", "OFS"):
                response = self.get(
                    "fnlttSinglAcntAll.json", {**params, "fs_div": basis}
                )
                if response["status"] == "013":
                    continue
                rows = response.get("list", [])
                if not rows or any(
                    row.get("rcept_no") != latest["rcept_no"] for row in rows
                ):
                    missing.append({**base, "reason": "receipt_mismatch_or_empty"})
                    break
                reports.append({**base, "statement_basis": basis, "accounts": rows})
                break
            else:
                missing.append({**base, "reason": "statements_unavailable"})
        return {
            "complete": True,
            "budget_used": self.used,
            "reports": reports,
            "missing": missing,
        }
