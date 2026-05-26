import json
import types
import uuid

import pytest

from app.services.action_report.remote_debug_audit.cdp_client import FakeCdpSession
from app.services.action_report.remote_debug_audit.cross_check import SymbolQuote
from app.services.action_report.remote_debug_audit.naver_quote import naver_url
from app.services.action_report.remote_debug_audit.service import (
    RemoteDebugAuditService,
    extract_symbol_quotes,
)


def _snap(kind, symbol=None, payload=None):
    return types.SimpleNamespace(
        snapshot_kind=kind, symbol=symbol, payload_json=payload or {}
    )


def test_extract_symbol_quotes_reads_quote_payload() -> None:
    pairs = [
        (
            object(),
            _snap(
                "symbol",
                "005930",
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "quote": {"status": "ok", "last_price": 81000.0},
                },
            ),
        ),
        (object(), _snap("market", None, {"foo": "bar"})),  # non-symbol ignored
        (
            object(),
            _snap(
                "symbol",
                "000660",
                {
                    "symbol": "000660",
                    "name": "SK하이닉스",
                    "quote": {"status": "unavailable"},
                },
            ),
        ),
    ]
    quotes = extract_symbol_quotes(pairs)
    assert quotes == [
        SymbolQuote("005930", "삼성전자", 81000.0, "ok"),
        SymbolQuote("000660", "SK하이닉스", None, "unavailable"),
    ]


class _FakeBundle:
    def __init__(self):
        self.id = 1
        self.bundle_uuid = uuid.uuid4()


class _FakeSnapshotsRepo:
    def __init__(self, bundle, pairs):
        self._bundle, self._pairs = bundle, pairs

    async def get_bundle_by_uuid(self, bundle_uuid):
        return self._bundle if bundle_uuid == self._bundle.bundle_uuid else None

    async def list_bundle_items_with_snapshots(self, bundle_id):
        return self._pairs


@pytest.mark.asyncio
async def test_audit_bundle_produces_findings_with_fake_cdp() -> None:
    bundle = _FakeBundle()
    pairs = [
        (
            object(),
            _snap(
                "symbol",
                "005930",
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "quote": {"status": "ok", "last_price": 81000.0},
                },
            ),
        ),
    ]
    cdp = FakeCdpSession(
        results={
            naver_url("005930"): json.dumps(
                {"code": "005930", "name": "삼성전자", "price_text": "81,300"}
            ),
        }
    )
    svc = RemoteDebugAuditService(
        snapshots_repo=_FakeSnapshotsRepo(bundle, pairs),
        reports_repo=None,
        cdp_session=cdp,
    )
    audit = await svc.audit_bundle(bundle.bundle_uuid, max_symbols=10)
    assert audit["checked_symbols"] == 1
    assert audit["findings"][0]["status"] == "ok"
    assert audit["affects_report_generation"] is False


@pytest.mark.asyncio
async def test_audit_bundle_per_symbol_failopen() -> None:
    bundle = _FakeBundle()
    pairs = [
        (
            object(),
            _snap(
                "symbol",
                "005930",
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "quote": {"status": "ok", "last_price": 81000.0},
                },
            ),
        ),
        (
            object(),
            _snap(
                "symbol",
                "999999",
                {
                    "symbol": "999999",
                    "quote": {"status": "ok", "last_price": 100.0},
                },
            ),
        ),
    ]
    # 999999 has no canned CDP result -> fetch raises -> finding unavailable, run continues.
    cdp = FakeCdpSession(
        results={
            naver_url("005930"): json.dumps(
                {"code": "005930", "name": "삼성전자", "price_text": "81,000"}
            ),
        }
    )
    svc = RemoteDebugAuditService(
        snapshots_repo=_FakeSnapshotsRepo(bundle, pairs),
        reports_repo=None,
        cdp_session=cdp,
    )
    audit = await svc.audit_bundle(bundle.bundle_uuid, max_symbols=10)
    assert audit["checked_symbols"] == 2
    statuses = {f["symbol"]: f["status"] for f in audit["findings"]}
    assert statuses["005930"] == "ok"
    assert statuses["999999"] == "unavailable"


@pytest.mark.asyncio
async def test_audit_bundle_missing_bundle_raises() -> None:
    bundle = _FakeBundle()
    svc = RemoteDebugAuditService(
        snapshots_repo=_FakeSnapshotsRepo(bundle, []),
        reports_repo=None,
        cdp_session=FakeCdpSession(results={}),
    )
    with pytest.raises(LookupError):
        await svc.audit_bundle(uuid.uuid4(), max_symbols=10)
