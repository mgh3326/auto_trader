"""``kiwoom_mock`` lane — B0-X KRX equities, **broker-truth** read/plan/execute.

Why a second KR module instead of a branch inside ``scripts.b0x.kr.mock``
--------------------------------------------------------------------------

§39차: ``kis_mock`` began rejecting every order with ``40910000 모의투자 주문이
불가한 계좌입니다`` — an *account-level* refusal, not a code defect. B0-X KR
therefore gets a **한시 대체** venue while participation is renewed. This is a
separate module, and the separation is the point:

* ``scripts/b0x/kr/mock.py`` (kis) is **untouched**. When the kis_mock account
  is restored it must behave exactly as it did before this file existed.
* The two lanes have different accounts, different order APIs, different
  artifact directories and — critically — different *evidence surfaces*.
  Folding them into one module would force the weaker surface's compromises
  onto the stronger one, which is precisely the mistake this file must not
  make (see "자기 미체결" below).

Only three broker classes are used, all of them mock-pinned:
:class:`~app.services.brokers.kiwoom.client.KiwoomMockClient` (transport, host
allowlist), :class:`~app.services.brokers.kiwoom.domestic_account.
KiwoomDomesticAccountClient` (reads) and :class:`~app.services.brokers.kiwoom.
domestic_orders.KiwoomDomesticOrderClient` (buy/sell/cancel). Nothing from the
``kis`` package, nothing from the MCP tool layer, nothing that can name the
live host.

🔴 자기 미체결 = 브로커 직접 조회. The v1.6 ① ledger exception does not apply here
-----------------------------------------------------------------------------

Contract v1.6 ① lets ``kis_mock`` substitute ``review.kis_mock_order_ledger``
for "what of mine is still resting?". That exception is scoped to a **missing
broker surface**: KIS 모의투자 미체결조회 (``TTTC8036R``) raises outright for
``is_mock=True``, and its daily-execution inquiry can answer ``rt_cd=0`` with
empty rows after real activity, so the venue genuinely cannot be asked.

Kiwoom **can** be asked. ``kt00009`` (계좌별주문체결현황요청) returns the account's
order/fill status rows, and ``kt00007`` (계좌별주문체결내역상세요청) returns per-order
detail. Reusing the kis ledger exception on a venue that answers would be a
contract violation, so this module reads :func:`read_broker_pending` from the
broker and nothing else. Falling into :class:`~scripts.b0x.broker_truth.
PendingUnreadable` is likewise **not** the normal path: it is reserved for a
genuinely failed/malformed read, which fails closed.

One deliberate widening, in the conservative direction: the symbol set fed to
``BrokerTruth.own_pending`` is the **account-wide** resting set, not just
B0-X's own rows. Kiwoom's order API accepts no client-supplied correlation id
(``kt10000``'s body is ``dmst_stex_tp``/``stk_cd``/``ord_qty``/``ord_uv``/
``trde_tp`` and nothing else), so "mine" can only be established through the
local order journal — and a resubmit gate that depends on a *local* record is
weaker than one that depends on the broker's own answer. Taking the superset
means the gate blocks strictly more than the contract requires and never less,
and it needs no local state to do it. Own-vs-account is still recorded
separately (:attr:`BrokerPending.own_symbols`) so the artifact never claims the
narrower fact it did not use. See :data:`OWN_PENDING_BASIS`.

🔴 legacy 보유 불가침 (§39차 ③, §36차 2항 패턴)
------------------------------------------------

``kiwoom_mock``'s operator lane is **KR-B1**; B0-X is a 한시 공존 배정. The
account may therefore carry holdings B0-X never created. Those are read,
counted and named — and excluded from every derivation input. The
own/legacy split reuses the KR core (:mod:`scripts.b0x.kr.attribution`'s pure
``scope_positions``/``assert_sell_is_own``) with a kiwoom-native evidence
source (:mod:`scripts.b0x.kr.kiwoom_attribution`); it never calls that
module's kis ledger reader.

NAV follows the #1835 precedent unchanged: ``nav = cash + 자기 귀속 평가금액``.
The §4 kill is ``pct_of_nav``, so including legacy market value would raise the
absolute loss tolerated before the kill fires. Excluding it can only narrow the
threshold, never widen it, and an unreadable attribution narrows it further
(attributed evaluation 0 → ``nav = cash``).

Two explicitly separate mutation modes
--------------------------------------

``ACCEPTANCE_ONLY`` submits **one** order and always tries to take it back:
submit → broker pending re-read → cancel → reconcile. There is no flag that
skips the cancel. It exists to prove the execution surface works end to end,
not to hold inventory. A cancel that fails is recorded as a failure and exits
non-zero — never laundered into a clean success (see
:class:`RoundTripIncomplete`).

``ORDERING`` uses :func:`submit_day_order` instead. That function
records only broker acceptance and the assigned order number; it deliberately
does not claim a fill and never invokes cancellation. The caller is responsible
for the stricter preflight and for choosing that non-default mode.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import os
import time
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Final
from urllib.parse import urlsplit

from app.core.config import settings, validate_kiwoom_mock_config
from app.mcp_server.tick_size import get_tick_size_kr
from app.services.brokers.kiwoom import constants as kiwoom_constants
from app.services.brokers.kiwoom.client import KiwoomMockClient
from app.services.brokers.kiwoom.domestic_account import KiwoomDomesticAccountClient
from app.services.brokers.kiwoom.domestic_orders import KiwoomDomesticOrderClient
from app.services.brokers.kiwoom.normalization import (
    normalize_deposit,
    normalize_order_detail,
    normalize_orders,
    normalize_positions,
)
from scripts.b0x.broker_truth import (
    BrokerTruth,
    PendingUnreadable,
    assert_resubmit_allowed,
)
from scripts.b0x.derivation import DerivedOrder
from scripts.b0x.envelope import Envelope, assert_envelope_locked
from scripts.b0x.kr.kiwoom_attribution import kst_order_date
from scripts.b0x.scope import KIWOOM_MOCK_SCOPE_KEY

LANE = KIWOOM_MOCK_SCOPE_KEY
MARKET = "kr"
QUOTE_CURRENCY = "KRW"

#: KRX trades whole shares. Same contract v1.2 dust rule the kis lane applies
#: (``scripts.b0x.kr.mock.KRX_MIN_TRADE_UNIT_SHARES``); defined locally so this
#: module does not import the kis lane, and pinned to that value by
#: ``tests/scripts/b0x/kr/kiwoom/test_kiwoom_shared_kr_semantics.py``.
KRX_MIN_TRADE_UNIT_SHARES: Final[Decimal] = Decimal("1")

#: ``b0xkw`` = B0-X KR **kiwoom**, deliberately distinct from the kis lane's
#: ``b0xk`` so neither lane's journal, artifact or ledger query can sweep in the
#: other's rows. ``b0xk`` is a prefix of ``b0xkw``, so every comparison against
#: it must be an exact-field match or a ``b0xkw-`` prefix match — never a
#: ``startswith("b0xk")``. :func:`assert_correlation_prefixes_disjoint` is the
#: regression guard.
CLIENT_ORDER_ID_PREFIX: Final[str] = "b0xkw"

#: The kis lane's prefix, quoted (not imported) purely so the disjointness
#: guard below has both literals in one place.
KIS_LANE_CLIENT_ORDER_ID_PREFIX: Final[str] = "b0xk"

_ENABLED_ENV = "B0X_KR_KIWOOM_ENABLED"

#: Recorded on every cycle. The contrast with the kis lane's
#: ``OWN_PENDING_SOURCE`` is the whole §39차 ② point and must stay visible in
#: the artifact rather than living only in this docstring.
OWN_PENDING_SOURCE: Final[str] = (
    "kt00007 계좌별주문체결내역상세 ord_remnq>0 (브로커 직접 조회) — 계약 v1.6 ① "
    "원장 예외 미사용. 🔴 kt00009 는 미체결이 있어도 빈 배열을 반환하는 것이 "
    "2026-08-12 실측되어 게이트에서 제외하고 진단으로만 기록한다"
)

#: Why ``BrokerTruth.own_pending`` carries the account-wide set. See the module
#: docstring: the widening is conservative and removes a dependency on local
#: state for a gate that must not depend on local state.
OWN_PENDING_BASIS: Final[str] = (
    "broker_account_wide_resting_superset — kiwoom 주문 API 는 클라이언트 "
    "correlation 을 받지 않으므로(kt10000 body 5필드) 「내 것」은 로컬 저널로만 "
    "성립한다. 재제출 게이트를 로컬 상태에 의존시키지 않으려고 계좌 전체 미체결을 "
    "쓴다 — 계약이 요구하는 자기 미체결의 상위집합이므로 항상 더 많이 막고 덜 막지 "
    "않는다. 자기 분(own_symbols)은 별도로 기록한다."
)


class KiwoomLaneDisabled(RuntimeError):
    """``B0X_KR_KIWOOM_ENABLED`` is not truthy, or kiwoom mock config is incomplete."""


class KiwoomHostViolation(RuntimeError):
    """A dispatch was about to leave for a host that is not ``mockapi.kiwoom.com``.

    Raised **before** any socket is opened. ``KiwoomMockClient`` already refuses
    a non-mock base URL in its constructor and re-checks the built request's
    host immediately before ``send``; this lane adds a third, lane-owned check
    so the promise does not rest on a single class staying correct.
    """


class KiwoomCashUnreadable(RuntimeError):
    """kt00001 carried no readable ``ord_alow_amt``.

    🔴 Distinct from "cash is zero", for the same reason the kis lane's
    :class:`~scripts.b0x.kr.mock.KrMockCashUnreadable` is: NAV is the
    denominator of the §4 ``pct_of_nav`` kill, so a fabricated ``0`` does not
    merely under-report — it moves the kill threshold. Never estimated.
    """


class KiwoomBrokerRejected(RuntimeError):
    """A Kiwoom response carried a non-zero ``return_code``.

    Kiwoom answers HTTP 200 with an in-body ``return_code``; treating that as
    success is the single easiest way to fake a green round trip.
    """

    def __init__(self, *, api: str, return_code: Any, return_msg: str) -> None:
        self.api = api
        self.return_code = return_code
        self.return_msg = return_msg
        super().__init__(
            f"kiwoom {api} rejected: return_code={return_code!r} msg={return_msg!r}"
        )


class BrokerEchoMismatch(RuntimeError):
    """The broker echoed a symbol/side/quantity/price we did not ask for.

    ROB-993 R3's lesson, reused: a submit or cancel response that does not echo
    the request is not evidence for the request. Fail closed rather than
    recording someone else's order as ours.
    """


class BrokerOrderReadbackUnavailable(RuntimeError):
    """The broker did not provide a complete, echoing post-submit readback.

    A successful ``kt10000``/``kt10001`` response is acknowledgement evidence,
    not lifecycle evidence.  ORDERING therefore does not promote a DAY order
    to any terminal state until the broker detail surface identifies the exact
    order and supplies fill/remaining fields.
    """


class RoundTripIncomplete(RuntimeError):
    """A submitted order could not be proven cancelled.

    🔴 This is the anti-laundering exception. An order that was sent and whose
    cancellation cannot be confirmed **from the broker's own answer** leaves the
    account in a state B0-X did not intend, and the only honest report is a
    failure. Never downgraded to a warning.
    """


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def assert_correlation_prefixes_disjoint() -> None:
    """Fail closed if the two KR lanes' correlation prefixes could be confused.

    ``b0xk`` is a string prefix of ``b0xkw``. Any query that matches with
    ``LIKE 'b0xk%'`` or ``startswith('b0xk')`` would therefore sweep in this
    lane's rows (and vice versa for a sloppy ``b0xkw`` match). The lanes are
    separated by always comparing against ``"<prefix>-"``, which *is* disjoint —
    this asserts that invariant instead of trusting it.
    """

    ours = f"{CLIENT_ORDER_ID_PREFIX}-"
    theirs = f"{KIS_LANE_CLIENT_ORDER_ID_PREFIX}-"
    if ours.startswith(theirs) or theirs.startswith(ours):
        raise AssertionError(
            f"KR lane correlation prefixes are not disjoint: {ours!r} vs {theirs!r} "
            "— a ledger/journal query for one lane would sweep in the other"
        )


def assert_kiwoom_lane_enabled() -> None:
    assert_correlation_prefixes_disjoint()
    if not _truthy(os.environ.get(_ENABLED_ENV)):
        raise KiwoomLaneDisabled(
            f"{_ENABLED_ENV} is not truthy. The B0-X kiwoom_mock lane is "
            "default-disabled; set it explicitly to arm this lane."
        )
    missing = validate_kiwoom_mock_config(settings)
    if missing:
        raise KiwoomLaneDisabled(f"Kiwoom mock config incomplete, missing: {missing}")


def account_identity_summary() -> dict[str, str]:
    """Report-safe fingerprint of the configured kiwoom mock account.

    Never emits the account number; only a one-way digest plus the two-character
    product suffix, matching the kis lane's convention.
    """

    compact = str(settings.kiwoom_mock_account_no or "").replace("-", "").strip()
    if len(compact) < 8:
        raise KiwoomLaneDisabled(
            "Kiwoom mock account identifier is unavailable or malformed"
        )
    return {
        "fingerprint": f"sha256:{hashlib.sha256(compact.encode()).hexdigest()[:16]}",
        "product_suffix": compact[-2:],
    }


def assert_mock_host(url: str) -> str:
    """Return ``url`` iff it is exactly the Kiwoom **mock** host.

    Third defence layer (see :class:`KiwoomHostViolation`). Deliberately
    compares against :data:`kiwoom_constants.MOCK_BASE_URL`'s host and *only*
    that host — the live constant is never consulted as an alternative, only as
    something to be absent.
    """

    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError as exc:  # pragma: no cover - urlsplit rarely raises
        raise KiwoomHostViolation(
            f"Kiwoom base URL is malformed and cannot be host-checked: {exc}"
        ) from exc
    expected = urlsplit(kiwoom_constants.MOCK_BASE_URL).netloc.lower()
    if netloc != expected:
        raise KiwoomHostViolation(
            "B0-X KR kiwoom is a mock-only lane; refusing to dispatch to host "
            f"{netloc or '(empty)'} — the only permitted host is {expected}"
        )
    return url


def _return_code(payload: dict[str, Any]) -> int | None:
    raw = payload.get("return_code")
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def assert_broker_ok(payload: dict[str, Any], *, api: str) -> dict[str, Any]:
    """Fail closed unless the broker reported ``return_code == 0``.

    A missing/unparseable ``return_code`` is also a rejection: an answer whose
    success field cannot be read is not a success.
    """

    code = _return_code(payload)
    if code != kiwoom_constants.SUCCESS_RETURN_CODE:
        raise KiwoomBrokerRejected(
            api=api,
            return_code=payload.get("return_code"),
            return_msg=str(payload.get("return_msg") or ""),
        )
    return payload


# ---------------------------------------------------------------------------
# Read-only account facade — mock host only.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawPosition:
    symbol: str
    quantity: Decimal
    average_price: Decimal
    evaluation_amount: Decimal


@dataclass(frozen=True, slots=True)
class RestingOrder:
    """One broker-reported resting (unfilled or partially filled) order.

    ``remaining_quantity`` is the broker's own ``ord_remnq`` (주문잔량) — the
    field the resting predicate is built on. ``unfilled_quantity``
    (``ord_qty - cntr_qty``) is carried alongside it as the arithmetic
    cross-check ``normalize_order_detail`` already computes: a partial cancel
    legitimately puts ``ord_remnq`` below the unfilled amount, so the two
    disagreeing is information, not an error.
    """

    order_id: str
    symbol: str
    status: str
    remaining_quantity: int
    ordered_price: int
    unfilled_quantity: int = 0


@dataclass(frozen=True, slots=True)
class BrokerPending:
    """kt00007's answer, split into account-wide and own.

    ``own_*`` is the intersection with the local order journal; ``account_*``
    is everything the broker reported. The gate consumes the account-wide set
    (see :data:`OWN_PENDING_BASIS`); both are recorded.
    """

    account_orders: tuple[RestingOrder, ...]
    own_order_ids: frozenset[str]

    @property
    def account_symbols(self) -> tuple[str, ...]:
        return tuple(sorted({order.symbol for order in self.account_orders}))

    @property
    def own_orders(self) -> tuple[RestingOrder, ...]:
        return tuple(
            order
            for order in self.account_orders
            if order.order_id in self.own_order_ids
        )

    @property
    def own_symbols(self) -> tuple[str, ...]:
        return tuple(sorted({order.symbol for order in self.own_orders}))

    @property
    def foreign_orders(self) -> tuple[RestingOrder, ...]:
        return tuple(
            order
            for order in self.account_orders
            if order.order_id not in self.own_order_ids
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "source": OWN_PENDING_SOURCE,
            "basis": OWN_PENDING_BASIS,
            "account_symbols": list(self.account_symbols),
            "account_order_count": len(self.account_orders),
            "own_symbols": list(self.own_symbols),
            "own_order_count": len(self.own_orders),
            "foreign_order_count": len(self.foreign_orders),
        }


def resting_orders_from_detail_rows(
    rows: list[dict[str, Any]],
) -> tuple[RestingOrder, ...]:
    """Derive the broker's current resting set from normalized ``kt00007`` rows.

    Keeping this pure lets a mutation boundary take *one* full same-day
    ``kt00007`` answer and derive both pending and foreign-trace gates from it;
    the two facts cannot then accidentally be taken from different moments.
    """

    resting: list[RestingOrder] = []
    for entry in rows:
        remaining = int(entry.get("remaining_quantity") or 0)
        if remaining <= 0:
            continue
        resting.append(
            RestingOrder(
                order_id=str(entry["order_id"]),
                symbol=str(entry["symbol"]),
                status=str(entry["status"]),
                remaining_quantity=remaining,
                ordered_price=int(entry.get("ordered_price") or 0),
                unfilled_quantity=int(entry.get("unfilled_quantity") or 0),
            )
        )
    return tuple(sorted(resting, key=lambda order: order.order_id))


def broker_pending_from_detail_rows(
    *, rows: list[dict[str, Any]], own_order_ids: frozenset[str]
) -> BrokerPending:
    """Build pending ownership facts from one normalized detail read."""

    return BrokerPending(
        account_orders=resting_orders_from_detail_rows(rows),
        own_order_ids=own_order_ids,
    )


#: 🔴 Minimum spacing between consecutive Kiwoom calls from this lane.
#:
#: Measured, not guessed. The 2026-08-03 mock rate probe recorded 2.0s/1.0s/0.5s
#: OK and 0.2s/0.05s failing with ``HTTPStatusError``, putting the threshold
#: between 0.5s and 0.2s. The first real acceptance attempt (2026-08-12
#: 12:11 KST) then issued nine calls in ~5s — roughly 0.55s apart, just inside
#: that "OK" band — and the ninth (the post-cancel reconcile read) still failed
#: with ``HTTPStatusError``. So the per-call spacing is not the whole story;
#: a burst of that length exceeds whatever window the mock enforces.
#:
#: 1.5s is chosen above the highest observed failure and below the point where
#: a full cycle (~10 calls ≈ 15s) could threaten the 5-minute NW-B4 preflight
#: window. It is deliberately a module constant with no CLI/env override: a
#: cycle that paces itself into a rate-limit failure loses the reconcile read,
#: and losing the reconcile read is exactly the state in which a submitted
#: order cannot be proven cancelled.
MIN_CALL_INTERVAL_SECONDS: Final[float] = 1.5


class ReadOnlyKiwoomMockAccount:
    """Account reads (+ the sanctioned order surface) over one mock client.

    The order client is constructed here rather than in a separate object so
    that every mutation shares this facade's host assertion, ``return_code``
    checking and rate pacing; there is no second, unchecked path to the venue.
    """

    def __init__(
        self,
        client: KiwoomMockClient | None = None,
        *,
        min_call_interval_seconds: float = MIN_CALL_INTERVAL_SECONDS,
    ) -> None:
        self._client = (
            KiwoomMockClient.from_app_settings() if client is None else client
        )
        assert_mock_host(kiwoom_constants.MOCK_BASE_URL)
        self._account = KiwoomDomesticAccountClient(self._client)
        self._orders = KiwoomDomesticOrderClient(self._client)
        self._min_interval = max(0.0, float(min_call_interval_seconds))
        self._last_call_at: float | None = None

    async def _pace(self) -> None:
        """Sleep until :data:`MIN_CALL_INTERVAL_SECONDS` has passed.

        Uses a monotonic clock so a wall-clock adjustment mid-cycle cannot
        collapse the spacing to zero.
        """

        if self._min_interval <= 0:
            return
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
        self._last_call_at = time.monotonic()

    # -- reads ------------------------------------------------------------

    async def read_cash(self) -> Decimal:
        await self._pace()
        payload = assert_broker_ok(await self._account.get_deposit(), api="kt00001")
        try:
            return Decimal(normalize_deposit(payload))
        except Exception as exc:  # noqa: BLE001 — unreadable is not zero
            raise KiwoomCashUnreadable(
                "kt00001 응답에서 주문가능금액(ord_alow_amt)을 읽지 못했다 — 조회 "
                "불가를 0 으로 읽으면 NAV 가 축소되어 §4 pct_of_nav kill 임계가 "
                f"왜곡되므로 추정하지 않고 fail-closed 한다 ({type(exc).__name__})"
            ) from exc

    async def read_positions(self) -> tuple[RawPosition, ...]:
        await self._pace()
        payload = assert_broker_ok(await self._account.get_balance(), api="kt00018")
        normalized = normalize_positions(payload)
        raw_rows = {
            _row_symbol(row): row
            for row in payload.get("acnt_evlt_remn_indv_tot") or []
            if isinstance(row, dict)
        }
        positions: list[RawPosition] = []
        for entry in normalized:
            symbol = str(entry["symbol"])
            quantity = Decimal(int(entry["quantity"]))
            if quantity <= 0:
                continue
            positions.append(
                RawPosition(
                    symbol=symbol,
                    quantity=quantity,
                    average_price=Decimal(int(entry["average_price"])),
                    evaluation_amount=_echoed_evaluation(raw_rows.get(symbol)),
                )
            )
        return tuple(sorted(positions, key=lambda pos: pos.symbol))

    async def read_resting_orders(
        self, *, order_date: str | None = None
    ) -> tuple[RestingOrder, ...]:
        """The account's currently resting orders, from **kt00007**.

        🔴 Why not kt00009, which is nominally *the* 미체결 surface — measured,
        2026-08-12 12:11–12:16 KST, on ``mockapi.kiwoom.com``:

            kt00009 (계좌별주문체결현황요청) answered ``return_code=0`` with an
            **empty row array** while two B0-X orders (``0107387``, ``0108695``)
            were live on the account, and kt00007 listed all four rows (both
            buys and both cancels) for the same day.

        An answer that is empty while orders rest cannot prove that none do —
        the exact property contract v1.5 ① disqualifies, and the same defect
        class that demoted KIS's ``inquire_daily_order_domestic`` to a
        non-gating diagnostic in ROB-341. Building the resubmit gate on
        kt00009 would have made it *vacuous*: every symbol would look free, and
        every "cancel confirmed" would be trivially true because the order was
        never in the list being checked.

        So the gate reads kt00007 (계좌별주문체결내역상세요청, ``qry_tp=1`` 주문순 for
        the trading day) and applies the broker's own ``ord_remnq`` as the
        resting predicate. kt00009 is still called — as a recorded
        **diagnostic**, never as a gate (see :meth:`read_order_status_diagnostic`).

        Anything that fails here must surface as
        :class:`~scripts.b0x.broker_truth.PendingUnreadable` at the caller,
        never as an empty tuple — see :func:`read_broker_pending`.
        """

        rows = await self.read_order_detail(
            order_date=order_date or kst_order_date(dt.datetime.now(dt.UTC))
        )
        return resting_orders_from_detail_rows(rows)

    async def read_order_status_diagnostic(self) -> dict[str, Any]:
        """kt00009, recorded but **never** used as a gate.

        Kept because the divergence between this surface and kt00007 is a real,
        reportable measurement about the mock (and the first real verification
        of the five-field body ROB-1088 landed unverified), and because a
        future mock fix should be visible as this count starting to match.
        """

        await self._pace()
        try:
            payload = assert_broker_ok(
                await self._account.get_order_status(), api="kt00009"
            )
            rows = normalize_orders(payload)
        except Exception as exc:  # noqa: BLE001 — diagnostic only, never gating
            return {"api": "kt00009", "readable": False, "error": type(exc).__name__}
        return {
            "api": "kt00009",
            "readable": True,
            "row_count": len(rows),
            "open_row_count": sum(
                1
                for row in rows
                if row["status"] in {"open", "partially_filled"}
                and int(row["remaining_quantity"]) > 0
            ),
        }

    async def read_order_detail(
        self, *, order_date: str | None = None, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """kt00007 → per-order detail rows (the fill-evidence surface).

        ``dmst_stex_tp`` is left at its KRX default: this lane is KRX-only and
        must not widen its read venue.
        """

        await self._pace()
        payload = assert_broker_ok(
            await self._account.get_order_detail(order_date=order_date, symbol=symbol),
            api="kt00007",
        )
        return normalize_order_detail(payload)

    # -- mutation surfaces -------------------------------------------------

    async def place_limit_buy(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        """kt10000. ``exchange`` is left at the KRX default deliberately.

        Passing an ``exchange`` argument at all would create the parameter a
        future caller could set to ``NXT``/``SOR``; the underlying client
        rejects those, and this lane additionally never offers the choice.
        """

        await self._pace()
        return assert_broker_ok(
            await self._orders.place_buy_order(
                symbol=symbol, quantity=quantity, price=price
            ),
            api="kt10000",
        )

    async def place_limit_sell(
        self, *, symbol: str, quantity: int, price: int
    ) -> dict[str, Any]:
        """kt10001. ``exchange`` remains fixed to the KRX default.

        This is reachable only after the cycle's attributed-holding boundary;
        exposing no exchange parameter keeps the NXT/SOR prohibition structural
        for both sides of an ORDERING DAY order.
        """

        await self._pace()
        return assert_broker_ok(
            await self._orders.place_sell_order(
                symbol=symbol, quantity=quantity, price=price
            ),
            api="kt10001",
        )

    async def cancel(
        self, *, original_order_no: str, symbol: str, cancel_quantity: int
    ) -> dict[str, Any]:
        """kt10003."""

        await self._pace()
        return assert_broker_ok(
            await self._orders.cancel_order(
                original_order_no=original_order_no,
                symbol=symbol,
                cancel_quantity=cancel_quantity,
            ),
            api="kt10003",
        )


def _row_symbol(row: dict[str, Any]) -> str:
    raw = str(row.get("stk_cd") or "").strip()
    if len(raw) == 7 and raw[0].upper() in {"A", "J", "Q"}:
        raw = raw[1:]
    return raw


#: kt00018 row fields that may carry a position's mark-to-market value, in the
#: order the official response table lists them.
_EVALUATION_FIELDS: tuple[str, ...] = ("evlt_amt", "evltv_amt", "evlu_amt")


def _echoed_evaluation(row: dict[str, Any] | None) -> Decimal:
    """Return the broker-echoed evaluation amount, or ``0`` when absent.

    🔴 ``0`` here is the *narrowing* choice, not an estimate: an unknown
    evaluation lowers NAV, which lowers the absolute §4 kill threshold. The
    opposite fallback (``quantity × average_price``) would invent a
    mark-to-market number and could only widen it.
    """

    if not isinstance(row, dict):
        return Decimal("0")
    for field_name in _EVALUATION_FIELDS:
        text = str(row.get(field_name, "")).replace(",", "").strip()
        if not text:
            continue
        try:
            value = Decimal(text)
        except Exception:  # noqa: BLE001 — unparseable is treated as absent
            continue
        return value if value > 0 else Decimal("0")
    return Decimal("0")


@dataclass(frozen=True, slots=True)
class FreshTruth:
    """Account-wide read-only snapshot for one cycle. Values stay here."""

    cash: Decimal
    nav: Decimal
    positions: tuple[RawPosition, ...]

    def non_dust_position_symbols(self) -> tuple[str, ...]:
        """Contract v1.5 ① 동시 포지션 — holdings that could become a SELL."""

        return tuple(
            sorted(
                pos.symbol
                for pos in self.positions
                if (pos.quantity // KRX_MIN_TRADE_UNIT_SHARES) >= 1
            )
        )

    def status_only(self, pending: BrokerPending | PendingUnreadable) -> dict[str, Any]:
        unreadable = pending if isinstance(pending, PendingUnreadable) else None
        return {
            "quote_currency": QUOTE_CURRENCY,
            "cash_present": bool(self.cash > 0),
            "nav_present": bool(self.nav > 0),
            "position_symbols": sorted(pos.symbol for pos in self.positions),
            "non_dust_position_symbols": list(self.non_dust_position_symbols()),
            "own_pending_readable": unreadable is None,
            "own_pending_unreadable_reason": (
                None if unreadable is None else unreadable.reason
            ),
            "own_pending_source": OWN_PENDING_SOURCE,
            "pending": (
                unreadable.canonical()
                if unreadable is not None
                else pending.canonical()  # type: ignore[union-attr]
            ),
        }


async def read_fresh_truth(account: ReadOnlyKiwoomMockAccount) -> FreshTruth:
    """Read-only cash + holdings snapshot, and the account-wide NAV from them.

    🔴 The NAV returned here is the **account-wide** one and is not what the
    kill switch consumes: :func:`scripts.b0x.kr.kiwoom_cycle.broker_state`
    rebuilds NAV as ``cash + 자기 귀속 평가금액`` so legacy market value never
    widens the ``pct_of_nav`` threshold. This value exists for the observation
    record only.
    """

    cash = await account.read_cash()
    positions = await account.read_positions()
    evaluation_total = sum(
        (pos.evaluation_amount for pos in positions), start=Decimal("0")
    )
    return FreshTruth(cash=cash, nav=cash + evaluation_total, positions=positions)


def pending_unreadable(cause: str) -> PendingUnreadable:
    """Tri-state for a failed kt00007 read.

    🔴 Not the normal path on this lane. ``kis_mock`` is *structurally*
    unreadable; kiwoom answers, so landing here means the read genuinely
    failed and every symbol is refused until it succeeds.
    """

    return PendingUnreadable(
        reason="kiwoom_mock_pending_read_failed",
        detail=(
            f"kt00007 계좌별주문체결내역상세 조회 실패({cause}) — kiwoom 은 미체결을 "
            "지원하므로 이것은 구조적 부재가 아니라 실패다. 「조회 실패」를 "
            "「미체결 없음」으로 읽지 않는다 (계약 v1.5 ①). 🔴 kis_mock 의 원장 "
            "예외(v1.6 ①)로 우회하지 않는다 — 그 예외는 브로커 표면 부재 한정이다"
        ),
    )


async def read_broker_pending(
    account: ReadOnlyKiwoomMockAccount,
    *,
    own_order_ids: frozenset[str],
    order_date: str | None = None,
) -> BrokerPending | PendingUnreadable:
    """자기 미체결 = 당일 브로커 resting 조회 (§39차 ②). Any failure → tri-state.

    `read_resting_orders` owns the current-day default. Keeping the wrapper
    here (rather than calling `read_order_detail(order_date=None)` directly)
    preserves the existing ACCEPTANCE_ONLY/readiness contract: an omitted date
    is a trading-day query, never a broker-default historical query.
    """

    try:
        resting = (
            await account.read_resting_orders()
            if order_date is None
            else await account.read_resting_orders(order_date=order_date)
        )
    except Exception as exc:  # noqa: BLE001 — any failure is "unknown"
        return pending_unreadable(type(exc).__name__)
    return BrokerPending(account_orders=resting, own_order_ids=own_order_ids)


def broker_truth_from(
    *,
    position_symbols: tuple[str, ...],
    pending: BrokerPending | PendingUnreadable,
) -> BrokerTruth:
    """Build the §4 cap inputs. Pending is broker-derived in both branches."""

    return BrokerTruth(
        position_symbols=position_symbols,
        own_pending=(
            pending
            if isinstance(pending, PendingUnreadable)
            else pending.account_symbols
        ),
    )


# ---------------------------------------------------------------------------
# Tick alignment + planning — pure, no network/DB call.
# ---------------------------------------------------------------------------


def align_price_kr(price: Decimal, *, side: str) -> int:
    """Snap to the runtime KRX tick, conservatively per side.

    Buys round down, sells round up. Semantically identical to
    ``scripts.b0x.kr.mock.align_price_kr`` and pinned to it by a sweep test —
    duplicated rather than imported so this module has no import edge into the
    kis lane (whose module scope constructs KIS order-adapter imports).
    """

    if price <= 0:
        raise ValueError("price must be > 0")
    tick = Decimal(get_tick_size_kr(float(price)))
    rounding = ROUND_DOWN if side == "buy" else ROUND_UP
    steps = (price / tick).quantize(Decimal("1"), rounding=rounding)
    aligned = steps * tick
    return max(1, int(aligned))


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    order_key: str
    client_order_id: str
    symbol: str
    side: str
    leg: str
    price: int
    quantity: int
    notional: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "order_key": self.order_key,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price": self.price,
            "quantity": self.quantity,
            "notional": format(self.notional, "f"),
        }


@dataclass(frozen=True, slots=True)
class BlockedOrder:
    order_key: str
    symbol: str
    leg: str
    reason: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {
            "order_key": self.order_key,
            "symbol": self.symbol,
            "leg": self.leg,
            "reason": self.reason,
            "detail": self.detail,
        }


def client_order_id_for(order_key: str) -> str:
    return f"{CLIENT_ORDER_ID_PREFIX}-{order_key}"


def plan_orders(
    orders: tuple[DerivedOrder, ...],
    *,
    envelope: Envelope,
    held_quantities: dict[str, Decimal],
) -> tuple[list[PlannedOrder], list[BlockedOrder]]:
    """Turn derived orders into whole-share KRW limit orders.

    Same rules as the kis lane (whole shares, floor never rounds up, realized
    notional re-checked against the cap after the floor). ``held_quantities``
    must already be **attribution-scoped**: a legacy holding must never reach
    this function as sellable inventory.
    """

    assert_envelope_locked(envelope)

    planned: list[PlannedOrder] = []
    blocked: list[BlockedOrder] = []

    for order in orders:
        if order.table_price <= 0:
            blocked.append(
                BlockedOrder(
                    order_key=order.order_key,
                    symbol=order.symbol,
                    leg=order.leg,
                    reason="non_positive_price",
                    detail=f"table_price={format(order.table_price, 'f')}",
                )
            )
            continue

        price = align_price_kr(order.table_price, side=order.side)

        if order.side == "buy":
            notional_cap = order.notional or envelope.per_order_notional
            quantity = int(
                (notional_cap / price).to_integral_value(rounding=ROUND_DOWN)
            )
            if quantity < 1:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason="sizing_blocked",
                        detail=(
                            f"notional={format(notional_cap, 'f')} price={price} "
                            "floors to < 1 share"
                        ),
                    )
                )
                continue
            realized_notional = Decimal(quantity) * price
            if realized_notional > envelope.per_order_notional:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason="envelope_violation_post_floor",
                        detail=(
                            f"realized notional={format(realized_notional, 'f')} > "
                            f"per-order cap "
                            f"{format(envelope.per_order_notional, 'f')} KRW"
                        ),
                    )
                )
                continue
        else:
            held = held_quantities.get(order.symbol, Decimal("0"))
            fraction = order.quantity_fraction or Decimal("0")
            quantity = int((held * fraction).to_integral_value(rounding=ROUND_DOWN))
            if quantity < 1:
                blocked.append(
                    BlockedOrder(
                        order_key=order.order_key,
                        symbol=order.symbol,
                        leg=order.leg,
                        reason="sizing_blocked",
                        detail=(
                            f"held={format(held, 'f')} "
                            f"fraction={format(fraction, 'f')} floors to < 1 share"
                        ),
                    )
                )
                continue
            realized_notional = Decimal(quantity) * price

        planned.append(
            PlannedOrder(
                order_key=order.order_key,
                client_order_id=client_order_id_for(order.order_key),
                symbol=order.symbol,
                side=order.side,
                leg=order.leg,
                price=price,
                quantity=quantity,
                notional=realized_notional,
            )
        )

    return planned, blocked


# ---------------------------------------------------------------------------
# Submission: DAY retention or mandatory acceptance cancel round trip.
# ---------------------------------------------------------------------------

#: Kiwoom's order response field carrying the assigned order number.
ORDER_NO_FIELD: Final[str] = "ord_no"


@dataclass
class DayOrderResult:
    """Broker-accepted KRX DAY order retained for ``ORDERING``.

    This records only what the submit response establishes: the request was
    accepted and assigned an order number. It is intentionally not a fill
    record, and its ``automatic_cancel`` field is a literal guard against
    accidentally reusing acceptance-round-trip semantics here.
    """

    correlation_id: str
    symbol: str
    side: str
    price: int
    quantity: int
    submitted: bool = False
    order_no: str | None = None
    submit_response: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "notional_krw": self.price * self.quantity,
            "submitted": self.submitted,
            "order_no": self.order_no,
            "submit_response": self.submit_response,
            "time_in_force": "DAY",
            "automatic_cancel": False,
            "fill_status": "unverified",
        }


@dataclass(frozen=True, slots=True)
class BrokerOrderReadback:
    """One exact broker-detail observation for an ORDERING DAY order.

    ``filled_quantity`` / ``remaining_quantity`` stay broker-derived.  The
    VWAP and slippage fields are only populated for an actual non-zero fill;
    an unfilled acknowledgement is neither a synthetic fill nor a completed
    lifecycle.
    """

    at: dt.datetime
    order_no: str
    symbol: str
    side: str
    intended_limit: int
    ordered_quantity: int
    filled_quantity: int
    remaining_quantity: int
    unfilled_quantity: int
    status: str
    fill_vwap: Decimal | None
    slippage_krw: Decimal | None

    @property
    def complete(self) -> bool:
        return (
            self.filled_quantity == self.ordered_quantity
            and self.remaining_quantity == 0
        )

    @property
    def partial(self) -> bool:
        return 0 < self.filled_quantity < self.ordered_quantity

    def canonical(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "order_no": self.order_no,
            "symbol": self.symbol,
            "side": self.side,
            "intended_limit": self.intended_limit,
            "ordered_quantity": self.ordered_quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "unfilled_quantity": self.unfilled_quantity,
            "status": self.status,
            "fill_vwap": (
                None if self.fill_vwap is None else format(self.fill_vwap, "f")
            ),
            "slippage_krw": (
                None if self.slippage_krw is None else format(self.slippage_krw, "f")
            ),
            "complete": self.complete,
            "partial": self.partial,
        }


@dataclass
class RoundTripResult:
    """Everything one acceptance round trip proved, and everything it did not."""

    correlation_id: str
    symbol: str
    side: str
    price: int
    quantity: int
    submitted: bool = False
    order_no: str | None = None
    #: The cancel is its own Kiwoom order and carries its own number; ``None``
    #: means the broker did not echo one (recorded, never guessed).
    cancel_order_no: str | None = None
    submit_response: dict[str, Any] = field(default_factory=dict)
    observed_resting: bool = False
    resting_snapshot: dict[str, Any] | None = None
    cancel_attempted: bool = False
    cancel_response: dict[str, Any] | None = None
    cancel_confirmed: bool = False
    reconcile_snapshot: dict[str, Any] | None = None
    failure: str | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "notional_krw": self.price * self.quantity,
            "submitted": self.submitted,
            "order_no": self.order_no,
            "cancel_order_no": self.cancel_order_no,
            "submit_response": self.submit_response,
            "observed_resting": self.observed_resting,
            "resting_snapshot": self.resting_snapshot,
            "cancel_attempted": self.cancel_attempted,
            "cancel_response": self.cancel_response,
            # 🔴 True only when a *post-cancel broker read* stopped reporting
            # the order as resting. The kt10003 response alone never sets it.
            "cancel_confirmed": self.cancel_confirmed,
            "reconcile_snapshot": self.reconcile_snapshot,
            "failure": self.failure,
            "round_trip_complete": bool(
                self.submitted and self.cancel_confirmed and self.failure is None
            ),
        }


def _extract_order_no(payload: dict[str, Any]) -> str:
    raw = str(payload.get(ORDER_NO_FIELD) or "").strip()
    if not raw or not raw.isdigit():
        raise BrokerEchoMismatch(
            "kt10000 응답에 사용 가능한 주문번호(ord_no)가 없다 — 주문번호 없이는 "
            "취소도 귀속도 불가능하므로 성공으로 기록하지 않는다 "
            f"(received={payload.get(ORDER_NO_FIELD)!r})"
        )
    return raw


def assert_resting_echo(
    order: RestingOrder, *, planned: PlannedOrder, order_no: str
) -> None:
    """Verify the broker's resting row is the order we actually sent."""

    problems: list[str] = []
    if order.order_id != order_no:
        problems.append(f"order_no {order.order_id!r} != {order_no!r}")
    if order.symbol != planned.symbol:
        problems.append(f"symbol {order.symbol!r} != {planned.symbol!r}")
    if order.remaining_quantity > planned.quantity:
        problems.append(
            f"remaining {order.remaining_quantity} > submitted {planned.quantity}"
        )
    if order.ordered_price and order.ordered_price != planned.price:
        problems.append(f"price {order.ordered_price} != {planned.price}")
    if problems:
        raise BrokerEchoMismatch(
            "kt00007 resting row does not echo the submitted order: "
            + "; ".join(problems)
        )


def _readback_from_detail_row(
    *,
    row: dict[str, Any],
    planned: PlannedOrder,
    order_no: str,
    at: dt.datetime,
) -> BrokerOrderReadback:
    """Validate and preserve one exact normalized ``kt00007`` row."""

    problems: list[str] = []
    if str(row.get("order_id") or "") != order_no:
        problems.append(f"order_no {row.get('order_id')!r} != {order_no!r}")
    if str(row.get("symbol") or "") != planned.symbol:
        problems.append(f"symbol {row.get('symbol')!r} != {planned.symbol!r}")
    if int(row.get("ordered_quantity") or 0) != planned.quantity:
        problems.append(
            f"ordered_quantity {row.get('ordered_quantity')!r} != {planned.quantity!r}"
        )
    if int(row.get("ordered_price") or 0) != planned.price:
        problems.append(
            f"ordered_price {row.get('ordered_price')!r} != {planned.price!r}"
        )
    filled = int(row.get("filled_quantity") or 0)
    remaining = int(row.get("remaining_quantity") or 0)
    unfilled = int(row.get("unfilled_quantity") or 0)
    if filled < 0 or remaining < 0 or unfilled < 0:
        problems.append("negative fill/remaining quantity")
    if filled > planned.quantity:
        problems.append(f"filled_quantity {filled} > submitted {planned.quantity}")
    if remaining > unfilled:
        problems.append(
            f"remaining_quantity {remaining} > unfilled_quantity {unfilled}"
        )
    if problems:
        raise BrokerEchoMismatch(
            "kt00007 order detail does not echo the submitted ORDERING request: "
            + "; ".join(problems)
        )

    fill_vwap: Decimal | None = None
    slippage: Decimal | None = None
    if filled > 0:
        fill_vwap = Decimal(int(row.get("average_price") or 0))
        if fill_vwap <= 0:
            raise BrokerOrderReadbackUnavailable(
                f"kt00007 reports filled_quantity={filled} for {order_no} but no "
                "positive broker VWAP; fill fidelity cannot be conserved"
            )
        # Positive is adverse slippage for both directions.
        slippage = (
            fill_vwap - Decimal(planned.price)
            if planned.side == "buy"
            else Decimal(planned.price) - fill_vwap
        )

    return BrokerOrderReadback(
        at=at,
        order_no=order_no,
        symbol=planned.symbol,
        side=planned.side,
        intended_limit=planned.price,
        ordered_quantity=planned.quantity,
        filled_quantity=filled,
        remaining_quantity=remaining,
        unfilled_quantity=unfilled,
        status=str(row.get("status") or "unknown"),
        fill_vwap=fill_vwap,
        slippage_krw=slippage,
    )


async def read_order_readback(
    account: ReadOnlyKiwoomMockAccount,
    *,
    planned: PlannedOrder,
    order_no: str,
    order_date: str,
    at: dt.datetime,
) -> BrokerOrderReadback:
    """Read the exact broker row after acknowledgement; absence fails closed."""

    try:
        rows = await account.read_order_detail(order_date=order_date)
    except Exception as exc:  # noqa: BLE001 — no readback is no lifecycle proof
        raise BrokerOrderReadbackUnavailable(
            f"kt00007 readback failed for {order_no} ({type(exc).__name__})"
        ) from exc
    row = next(
        (item for item in rows if str(item.get("order_id") or "") == order_no), None
    )
    if row is None:
        raise BrokerOrderReadbackUnavailable(
            f"kt00007 readback did not return acknowledged order {order_no}"
        )
    return _readback_from_detail_row(row=row, planned=planned, order_no=order_no, at=at)


async def submit_day_order(
    account: ReadOnlyKiwoomMockAccount,
    *,
    planned: PlannedOrder,
    broker_truth: BrokerTruth,
    record_order_no: Any,
    now: dt.datetime,
) -> DayOrderResult:
    """Submit one KRX limit order and deliberately leave its DAY lifecycle open.

    The pre-dispatch resubmit gate and immediate journal append are identical
    to the acceptance path. What differs is deliberate: no post-submit
    cancellation, no inferred fill, and no claim that a still-open order is a
    successful reconciliation.
    """

    assert_resubmit_allowed(broker_truth, symbol=planned.symbol, lane=LANE)
    result = DayOrderResult(
        correlation_id=planned.client_order_id,
        symbol=planned.symbol,
        side=planned.side,
        price=planned.price,
        quantity=planned.quantity,
    )

    if planned.side == "buy":
        submit_payload = await account.place_limit_buy(
            symbol=planned.symbol, quantity=planned.quantity, price=planned.price
        )
    elif planned.side == "sell":
        submit_payload = await account.place_limit_sell(
            symbol=planned.symbol, quantity=planned.quantity, price=planned.price
        )
    else:
        raise ValueError(f"kiwoom day order has unsupported side: {planned.side}")

    result.submitted = True
    result.submit_response = dict(submit_payload)
    order_no = _extract_order_no(submit_payload)
    result.order_no = order_no
    # A later same-day foreign-trace check must know that this broker order is
    # ours even while it remains resting; write before returning to the caller.
    record_order_no(order_no=order_no, planned=planned, at=now)
    return result


async def submit_and_cancel(
    account: ReadOnlyKiwoomMockAccount,
    *,
    planned: PlannedOrder,
    broker_truth: BrokerTruth,
    record_order_no: Any,
    now: dt.datetime,
) -> RoundTripResult:
    """Submit one planned BUY, prove it rests, cancel it, prove it is gone.

    ``record_order_no(order_no, planned, at)`` is the journal writer; it is
    called **immediately** after the broker returns an order number and before
    anything else can fail, because an unrecorded order number is an order this
    lane can no longer attribute or cancel on a later run.

    Raises:
        RoundTripIncomplete: when the order was sent but its cancellation could
            not be proven from a post-cancel broker read.
    """

    if planned.side != "buy":
        # The acceptance lever is buy-only by construction. A sell would have
        # to clear the legacy gate, and this function is not where that
        # judgement belongs — ``kiwoom_cycle`` refuses it before here.
        raise ValueError(
            f"kiwoom acceptance round trip is buy-only; got {planned.side}"
        )

    assert_resubmit_allowed(broker_truth, symbol=planned.symbol, lane=LANE)

    result = RoundTripResult(
        correlation_id=planned.client_order_id,
        symbol=planned.symbol,
        side=planned.side,
        price=planned.price,
        quantity=planned.quantity,
    )

    submit_payload = await account.place_limit_buy(
        symbol=planned.symbol, quantity=planned.quantity, price=planned.price
    )
    result.submitted = True
    result.submit_response = dict(submit_payload)
    order_no = _extract_order_no(submit_payload)
    result.order_no = order_no
    record_order_no(order_no=order_no, planned=planned, at=now)

    # --- prove it rests (this is the surface kis_mock does not have) ---
    try:
        resting = await account.read_resting_orders()
    except Exception as exc:  # noqa: BLE001
        result.failure = f"pending_read_failed:{type(exc).__name__}"
        resting = ()
    else:
        match = next((row for row in resting if row.order_id == order_no), None)
        if match is not None:
            assert_resting_echo(match, planned=planned, order_no=order_no)
            result.observed_resting = True
            result.resting_snapshot = {
                "order_id": match.order_id,
                "symbol": match.symbol,
                "status": match.status,
                "remaining_quantity": match.remaining_quantity,
                "ordered_price": match.ordered_price,
            }
        else:
            # Not an error by itself — a marketable limit can fill instantly —
            # but it must be recorded as "not observed resting", never as a
            # cancellation.
            result.resting_snapshot = None

    # --- cancel: always attempted once the order exists ---
    cancel_quantity = (
        result.resting_snapshot["remaining_quantity"]
        if result.resting_snapshot
        else planned.quantity
    )
    result.cancel_attempted = True
    try:
        result.cancel_response = dict(
            await account.cancel(
                original_order_no=order_no,
                symbol=planned.symbol,
                cancel_quantity=int(cancel_quantity),
            )
        )
    except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
        result.cancel_response = {
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }
    else:
        # 🔴 A Kiwoom cancel is itself an order and gets its **own** ``ord_no``
        # (observed 2026-08-12: buy ``0107387`` → cancel ``0107388``). Journal
        # it too, or the next cycle's same-day foreign-trace gate reads this
        # lane's own cancel as a second writer and refuses to start — which is
        # exactly what happened on the first acceptance attempt. The record is
        # written with ``side="cancel"``: :func:`kiwoom_attribution.
        # build_attribution` counts only ``buy``/``sell``, so a cancel row
        # establishes *ownership* without ever moving an attributed quantity.
        try:
            cancel_order_no = _extract_order_no(result.cancel_response)
        except BrokerEchoMismatch:
            cancel_order_no = None
        result.cancel_order_no = cancel_order_no
        if cancel_order_no is not None and cancel_order_no != order_no:
            record_order_no(
                order_no=cancel_order_no,
                planned=replace(planned, side="cancel"),
                at=now,
            )

    # --- reconcile: the ONLY thing that may set cancel_confirmed ---
    try:
        after = await account.read_resting_orders()
    except Exception as exc:  # noqa: BLE001
        result.failure = f"reconcile_read_failed:{type(exc).__name__}"
        raise RoundTripIncomplete(
            f"order {order_no} was submitted but the post-cancel broker read "
            f"failed ({type(exc).__name__}) — cancellation is unproven and this "
            "run must not be reported as clean"
        ) from exc

    still_resting = next((row for row in after if row.order_id == order_no), None)
    result.reconcile_snapshot = {
        "account_resting_order_count": len(after),
        "this_order_still_resting": still_resting is not None,
        "this_order_remaining_quantity": (
            None if still_resting is None else still_resting.remaining_quantity
        ),
    }
    result.cancel_confirmed = still_resting is None
    if not result.cancel_confirmed:
        result.failure = "cancel_unconfirmed"
        raise RoundTripIncomplete(
            f"order {order_no} ({planned.symbol}) is still reported as resting by "
            f"kt00007 after kt10003 — remaining="
            f"{still_resting.remaining_quantity if still_resting else '?'}. "
            "Refusing to report a cancellation the broker did not confirm."
        )
    return result


__all__ = [
    "LANE",
    "MARKET",
    "QUOTE_CURRENCY",
    "CLIENT_ORDER_ID_PREFIX",
    "KIS_LANE_CLIENT_ORDER_ID_PREFIX",
    "KRX_MIN_TRADE_UNIT_SHARES",
    "ORDER_NO_FIELD",
    "OWN_PENDING_BASIS",
    "OWN_PENDING_SOURCE",
    "BlockedOrder",
    "BrokerEchoMismatch",
    "BrokerOrderReadback",
    "BrokerOrderReadbackUnavailable",
    "BrokerPending",
    "DayOrderResult",
    "FreshTruth",
    "KiwoomBrokerRejected",
    "KiwoomCashUnreadable",
    "KiwoomHostViolation",
    "KiwoomLaneDisabled",
    "PlannedOrder",
    "RawPosition",
    "ReadOnlyKiwoomMockAccount",
    "RestingOrder",
    "RoundTripIncomplete",
    "RoundTripResult",
    "align_price_kr",
    "account_identity_summary",
    "assert_broker_ok",
    "assert_correlation_prefixes_disjoint",
    "assert_kiwoom_lane_enabled",
    "assert_mock_host",
    "assert_resting_echo",
    "broker_pending_from_detail_rows",
    "broker_truth_from",
    "client_order_id_for",
    "pending_unreadable",
    "plan_orders",
    "read_broker_pending",
    "read_fresh_truth",
    "read_order_readback",
    "resting_orders_from_detail_rows",
    "submit_day_order",
    "submit_and_cancel",
]
