"""Exact-account KR mock position proof; every doubt is unproven, never zero."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.brokers.kis import constants

MAX_BALANCE_PAGES = 10
MAX_BASELINE_AGE = timedelta(seconds=300)
MAX_READ_AGE = timedelta(seconds=60)
_FINAL_TR_CONT = frozenset({"D", "E", ""})
_CONTINUE_TR_CONT = frozenset({"F", "M"})


class PositionUnproven(Exception):
    """A closed reason code; carries no broker payload or account value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    symbol: str
    quantity: int
    observed_at: datetime
    read_digest: str


@dataclass(frozen=True, slots=True)
class PositionProof:
    side: str
    baseline_quantity: int
    filled_quantity: int
    observed_quantity: int
    read_digest: str


def _tr_cont(headers: object) -> str | None:
    if not isinstance(headers, dict):
        return None
    values = [v for k, v in headers.items() if str(k).lower() == "tr_cont"]
    if len(values) != 1 or type(values[0]) is not str:
        return None
    return values[0].strip().upper()


def _holding(value: object) -> int:
    if type(value) is str and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise PositionUnproven("position_read_malformed")


def parse_balance_pages(
    pages: Sequence[tuple[dict[str, Any], dict[str, str]]],
    *,
    symbol: str,
    observed_at: datetime,
) -> BalanceSnapshot:
    """Read one symbol from a complete page sequence.

    KIS omits empty holdings, so a symbol is zero only when every page was
    well formed and the stream positively ended.
    """
    if not pages:
        raise PositionUnproven("position_read_malformed")
    rows: list[dict[str, Any]] = []
    last_cont: str | None = None
    for payload, headers in pages:
        output1, output2 = (
            payload.get("output1") if type(payload) is dict else None,
            payload.get("output2") if type(payload) is dict else None,
        )
        if (
            type(payload) is not dict
            or payload.get("rt_cd") != "0"
            or type(output1) is not list
            or any(type(row) is not dict for row in output1)
            or type(output2) is not list
            or not output2
        ):
            raise PositionUnproven("position_read_malformed")
        rows.extend(output1)
        last_cont = _tr_cont(headers)
    if last_cont not in _FINAL_TR_CONT:
        raise PositionUnproven("position_read_truncated")
    if any(type(row.get("pdno")) is not str for row in rows):
        raise PositionUnproven("position_read_malformed")
    matching = [row for row in rows if row["pdno"] == symbol]
    if len(matching) > 1:
        raise PositionUnproven("position_read_ambiguous")
    quantity = _holding(matching[0].get("hldg_qty")) if matching else 0
    digest = hashlib.sha256(
        json.dumps({"symbol": symbol, "quantity": quantity}, sort_keys=True).encode()
    ).hexdigest()
    return BalanceSnapshot(symbol, quantity, observed_at, digest)


def expected_quantity(*, side: str, baseline: int, filled: int) -> int:
    if side not in {"buy", "sell"}:
        raise PositionUnproven("position_side_invalid")
    expected = baseline + filled if side == "buy" else baseline - filled
    if expected < 0:
        raise PositionUnproven("position_mismatch")
    return expected


def reconcile_position(
    *,
    side: str,
    baseline: int,
    filled: int,
    snapshot: BalanceSnapshot,
    symbol: str,
    acknowledged_at: datetime,
    now: datetime,
) -> PositionProof:
    if type(snapshot) is not BalanceSnapshot or snapshot.symbol != symbol:
        raise PositionUnproven("position_read_malformed")
    if (
        snapshot.observed_at < acknowledged_at
        or snapshot.observed_at > now
        or now - snapshot.observed_at > MAX_READ_AGE
    ):
        raise PositionUnproven("position_read_stale")
    if snapshot.quantity != expected_quantity(
        side=side, baseline=baseline, filled=filled
    ):
        raise PositionUnproven("position_mismatch")
    return PositionProof(
        side, baseline, filled, snapshot.quantity, snapshot.read_digest
    )


async def read_complete_domestic_balance(client: Any) -> list[tuple[dict, dict]]:
    """Fetch every VTTC8434R page; a page cap is a truncated read."""
    await client._ensure_token()
    account_no = re.sub(r"\D", "", client._settings.kis_account_no)
    if len(account_no) != 10:
        raise PositionUnproven("position_read_malformed")
    pages: list[tuple[dict, dict]] = []
    fk, nk, request_cont = "", "", ""
    for _ in range(MAX_BALANCE_PAGES):
        payload, headers = await client._request_with_rate_limit_with_headers(
            "GET",
            client._kis_url(constants.DOMESTIC_BALANCE_URL),
            headers=client._hdr_base
            | {
                "authorization": f"Bearer {client._settings.kis_access_token}",
                "tr_id": constants.DOMESTIC_BALANCE_TR_MOCK,
                "tr_cont": request_cont,
            },
            params={
                "CANO": account_no[:8],
                "ACNT_PRDT_CD": account_no[8:],
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "00",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": fk,
                "CTX_AREA_NK100": nk,
            },
            timeout=10,
            api_name="kis_mock_position_proof",
            tr_id=constants.DOMESTIC_BALANCE_TR_MOCK,
        )
        pages.append((payload, headers))
        cont = _tr_cont(headers)
        if cont not in _CONTINUE_TR_CONT:
            return pages
        new_nk = payload.get("CTX_AREA_NK100") if type(payload) is dict else None
        if type(new_nk) is not str or not new_nk.strip() or new_nk == nk:
            raise PositionUnproven("position_read_truncated")
        fk, nk, request_cont = payload.get("CTX_AREA_FK100") or "", new_nk, "N"
    raise PositionUnproven("position_read_truncated")


class KISMockPositionReader:
    """Lazily built, identity-checked, read-only balance port for one recovery run."""

    def __init__(self, entry: Any) -> None:
        self._entry = entry
        self._client: Any = None

    def _checked_client(self) -> Any:
        from app.services.brokers.kis import KISClient
        from app.services.kis_mock_runner.singleton import kis_mock_account_fingerprint
        from app.services.mock_lane_registry import assert_mock_only_endpoint

        if self._client is None:
            self._client = KISClient(is_mock=True)
        client = self._client
        view = client._settings
        if (
            client._is_mock_client is not True
            or getattr(view, "_is_mock", None) is not True
            or assert_mock_only_endpoint(self._entry, view.kis_base_url)
            != "openapivts.koreainvestment.com:29443"
            or kis_mock_account_fingerprint(
                app_key=view.kis_app_key, account_no=view.kis_account_no
            )
            != self._entry.physical_account_id
        ):
            raise PositionUnproven("position_identity_mismatch")
        return client

    async def __call__(self, symbol: str) -> BalanceSnapshot:
        pages = await read_complete_domestic_balance(self._checked_client())
        return parse_balance_pages(pages, symbol=symbol, observed_at=datetime.now(UTC))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
