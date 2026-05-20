"""ROB-268 — KIS domestic balance snapshot helper tests.

Validates that ``AccountClient.fetch_domestic_balance_snapshot`` consolidates
holdings (output1) and cash (output2) from a single KIS inquire-balance
response lineage, and uses the response header ``tr_cont`` (not just the body
cursor) to decide whether to request another page.

Reference: see Linear ROB-268 for the original Sentry trace evidence
(``invest.home.kis_mock`` ~14.3s of 15s total) showing the duplicate-call
pattern this helper replaces.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.brokers.kis import constants
from app.services.brokers.kis.account import AccountClient


class _FakeSettings:
    kis_account_no = "12345678-01"
    kis_access_token = "test-token"
    kis_app_key = "key"
    kis_app_secret = "secret"


class _FakeParent:
    """Fake KIS parent client.

    The snapshot helper is expected to drive paging through a new header-aware
    request method ``_request_with_rate_limit_with_headers`` so that the
    response header ``tr_cont`` becomes visible to the caller. Tests configure
    a queue of ``(body, headers)`` pairs that are returned in order.
    """

    def __init__(
        self,
        responses: list[tuple[dict[str, Any], dict[str, str]]],
    ) -> None:
        self._settings = _FakeSettings()
        self._hdr_base = {
            "appkey": "key",
            "appsecret": "secret",
            "tr_id": "X",
            "custtype": "P",
        }
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses)
        self._ensure_token = AsyncMock()

        async def _stub(
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, Any] | None = None,
            json_body: dict[str, Any] | None = None,
            timeout: float = 5.0,
            api_name: str = "unknown",
            tr_id: str | None = None,
            retry_request_errors: bool = True,
        ) -> tuple[dict[str, Any], dict[str, str]]:
            self.calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": dict(headers),
                    "params": dict(params or {}),
                    "api_name": api_name,
                    "tr_id": tr_id,
                    "retry_request_errors": retry_request_errors,
                }
            )
            if not self._responses:
                raise AssertionError(
                    "Unexpected KIS request: ran out of queued responses "
                    f"(call #{len(self.calls)})"
                )
            return self._responses.pop(0)

        self._request_with_rate_limit_with_headers = _stub

    def _kis_url(self, path: str) -> str:
        return f"https://example.com{path}"


def _page(
    *,
    stocks: list[dict[str, Any]] | None = None,
    cash: dict[str, Any] | None = None,
    ctx_fk: str = "",
    ctx_nk: str = "",
    tr_cont: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    body: dict[str, Any] = {
        "rt_cd": "0",
        "msg_cd": "",
        "msg1": "",
        "output1": stocks or [],
        "output2": [cash] if cash is not None else [{}],
        "CTX_AREA_FK100": ctx_fk,
        "CTX_AREA_NK100": ctx_nk,
    }
    return body, {"tr_cont": tr_cont}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_single_page_issues_one_request() -> None:
    """Normal 1-page case: exactly one /inquire-balance call."""
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[
                    {
                        "pdno": "005930",
                        "prdt_name": "삼성전자",
                        "hldg_qty": "1",
                        "pchs_avg_pric": "70000",
                        "pchs_amt": "70000",
                        "evlu_amt": "72000",
                    }
                ],
                cash={
                    "dnca_tot_amt": "1000000",
                    "stck_cash_ord_psbl_amt": "900000",
                },
                tr_cont="D",
            )
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert len(parent.calls) == 1, (
        f"Expected exactly 1 inquire-balance call, got {len(parent.calls)}"
    )
    assert snapshot["page_count"] == 1
    assert len(snapshot["holdings"]) == 1
    assert snapshot["holdings"][0]["pdno"] == "005930"
    assert snapshot["cash"]["dnca_tot_amt"] == "1000000"
    assert snapshot["cash"]["stck_cash_ord_psbl_amt"] == "900000"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_does_not_page_when_tr_cont_is_end() -> None:
    """Phantom page guard: body cursor is non-empty but tr_cont is end → stop.

    Reproduces the ROB-268 root cause: KIS VTS sometimes returns a non-empty
    CTX_AREA_NK100 even on the last page. Pre-fix code looked only at the
    body cursor and requested a phantom page 2.
    """
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[{"pdno": "A", "hldg_qty": "1"}],
                ctx_fk="LEFTOVER_FK",
                ctx_nk="LEFTOVER_NK",
                tr_cont="D",  # end-of-stream
            )
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert len(parent.calls) == 1, (
        "Must not issue page 2 when response header tr_cont signals end, "
        "even when body cursor is non-empty"
    )
    assert snapshot["page_count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_pages_when_tr_cont_is_continuation() -> None:
    """tr_cont=F (or M) with a new cursor → issue the next page."""
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[{"pdno": "A", "hldg_qty": "1"}],
                ctx_fk="FK1",
                ctx_nk="NK1",
                tr_cont="F",
            ),
            _page(
                stocks=[{"pdno": "B", "hldg_qty": "2"}],
                ctx_fk="",
                ctx_nk="",
                tr_cont="D",
            ),
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert len(parent.calls) == 2
    # Second request must echo the page-1 cursor back and set tr_cont=N
    assert parent.calls[1]["params"]["CTX_AREA_FK100"] == "FK1"
    assert parent.calls[1]["params"]["CTX_AREA_NK100"] == "NK1"
    assert parent.calls[1]["headers"]["tr_cont"] == "N"
    assert snapshot["page_count"] == 2
    assert {h["pdno"] for h in snapshot["holdings"]} == {"A", "B"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_cash_comes_from_first_page_only() -> None:
    """Cash (output2) is captured from page 1; later pages do not override."""
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[{"pdno": "A", "hldg_qty": "1"}],
                cash={"dnca_tot_amt": "100", "stck_cash_ord_psbl_amt": "90"},
                ctx_fk="FK1",
                ctx_nk="NK1",
                tr_cont="M",
            ),
            _page(
                stocks=[{"pdno": "B", "hldg_qty": "1"}],
                cash={
                    "dnca_tot_amt": "999999",
                    "stck_cash_ord_psbl_amt": "888888",
                },
                tr_cont="D",
            ),
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert snapshot["cash"]["dnca_tot_amt"] == "100"
    assert snapshot["cash"]["stck_cash_ord_psbl_amt"] == "90"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_stops_on_repeated_cursor() -> None:
    """Defensive: even with tr_cont=M, a repeated cursor must terminate paging."""
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[{"pdno": "A", "hldg_qty": "1"}],
                ctx_fk="FK1",
                ctx_nk="NK1",
                tr_cont="F",
            ),
            _page(
                stocks=[{"pdno": "B", "hldg_qty": "1"}],
                ctx_fk="FK1",
                ctx_nk="NK1",  # repeated cursor
                tr_cont="M",
            ),
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert len(parent.calls) == 2, (
        "Must terminate when KIS returns the same cursor twice in a row"
    )
    assert snapshot["page_count"] == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_returns_stop_reason_tr_cont_end() -> None:
    """stop_reason is part of the snapshot contract used for Sentry tagging."""
    parent = _FakeParent(
        responses=[_page(stocks=[{"pdno": "A", "hldg_qty": "1"}], tr_cont="D")]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert snapshot["stop_reason"] == "tr_cont_end"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_returns_stop_reason_empty_cursor() -> None:
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[{"pdno": "A", "hldg_qty": "1"}],
                ctx_nk="",
                tr_cont="F",
            )
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert snapshot["stop_reason"] == "empty_cursor"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_returns_stop_reason_repeated_cursor() -> None:
    parent = _FakeParent(
        responses=[
            _page(
                stocks=[{"pdno": "A", "hldg_qty": "1"}],
                ctx_nk="NK1",
                tr_cont="F",
            ),
            _page(
                stocks=[{"pdno": "B", "hldg_qty": "1"}],
                ctx_nk="NK1",
                tr_cont="M",
            ),
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert snapshot["stop_reason"] == "repeated_cursor"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_uses_mock_tr_id_when_is_mock_true() -> None:
    parent = _FakeParent(responses=[_page(stocks=[], tr_cont="")])
    client = AccountClient(parent)

    await client.fetch_domestic_balance_snapshot(is_mock=True)

    assert parent.calls[0]["tr_id"] == constants.DOMESTIC_BALANCE_TR_MOCK


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_uses_real_tr_id_when_is_mock_false() -> None:
    parent = _FakeParent(responses=[_page(stocks=[], tr_cont="")])
    client = AccountClient(parent)

    await client.fetch_domestic_balance_snapshot(is_mock=False)

    assert parent.calls[0]["tr_id"] == constants.DOMESTIC_BALANCE_TR


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_propagates_timeout_and_retry_flag_to_request() -> None:
    """ROB-270: fetch_domestic_balance_snapshot honors per-call timeout and
    retry_request_errors when explicitly passed, defaulting to live values
    otherwise."""
    parent = _FakeParent(
        responses=[
            _page(stocks=[], cash={}, tr_cont="D"),
        ]
    )
    # Wrap stub to capture the actually-passed kwargs
    original_stub = parent._request_with_rate_limit_with_headers
    captured: dict[str, Any] = {}

    async def _capturing_stub(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return await original_stub(*args, **kwargs)

    parent._request_with_rate_limit_with_headers = _capturing_stub
    client = AccountClient(parent)

    await client.fetch_domestic_balance_snapshot(
        is_mock=True,
        timeout=10.0,
        retry_request_errors=False,
    )

    assert captured.get("timeout") == pytest.approx(10.0)
    assert captured.get("retry_request_errors") is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_defaults_preserve_live_request_policy() -> None:
    """ROB-270: Live default — timeout=5, retry_request_errors=True."""
    parent = _FakeParent(
        responses=[
            _page(stocks=[], cash={}, tr_cont="D"),
        ]
    )
    captured: dict[str, Any] = {}
    original = parent._request_with_rate_limit_with_headers

    async def _capturing_stub(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return await original(*args, **kwargs)

    parent._request_with_rate_limit_with_headers = _capturing_stub
    client = AccountClient(parent)

    await client.fetch_domestic_balance_snapshot()  # all defaults

    assert captured.get("timeout") == pytest.approx(5.0)
    assert captured.get("retry_request_errors", True) is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_honors_explicit_max_pages_cap() -> None:
    """ROB-270: max_pages param can shrink the live default cap for mock UI."""
    # Two continuation pages then end — but with max_pages=1 we stop after 1.
    parent = _FakeParent(
        responses=[
            _page(stocks=[{"pdno": "A", "hldg_qty": "1"}], ctx_nk="X", tr_cont="F"),
            # If max_pages is honored we never reach this response.
            _page(stocks=[{"pdno": "B", "hldg_qty": "1"}], tr_cont="D"),
        ]
    )
    client = AccountClient(parent)

    snapshot = await client.fetch_domestic_balance_snapshot(is_mock=True, max_pages=1)

    assert snapshot["page_count"] == 1
    assert len(parent.calls) == 1
    assert snapshot["holdings"][0]["pdno"] == "A"
