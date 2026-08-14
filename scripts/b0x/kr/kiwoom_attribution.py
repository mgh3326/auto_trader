"""``kiwoom_mock`` 자기 귀속 — legacy 보유와 B0-X 보유를 가르는 경계 (§39차 ③).

왜 원장이 아니라 「저널 + 브로커 체결」인가
--------------------------------------------

``kiwoom_mock`` 의 operator lane 은 **KR-B1** 이고 B0-X 는 §39차 한시 **공존**
배정이다. 계좌에 B0-X 가 만들지 않은 보유(legacy)가 있을 수 있으며, 그것을
매도하면 **남의 포지션 처분**이다 — 이 레인에서 되돌릴 수 없는 유일한 사고.

#1835(§36차 2항)가 kis 레인에 세운 원칙은 그대로 가져온다:

    B0-X 매도/물타기 파생 = 자기 체결(fill) 귀속 포지션 한정
    legacy = 불가침 무시 (읽되 파생 입력에서 제외, 매도 경로 부재)
    과소 귀속 = 매도 못 함 = 안전 / 과대 귀속 = legacy 매도 = 사고

바뀌는 것은 **증거 원천 하나**뿐이다. kis 레인은
``review.kis_mock_order_ledger`` 를 쓴다. 🔴 이 모듈은 그 원장을 쓰지 않는다 —
계약 v1.6 ① 은 「브로커 표면 부재」 한정 예외이고 kiwoom 에는 표면이 있다.
:mod:`scripts.b0x.kr.attribution` 에서 가져오는 것은 **순수 함수**
(``scope_positions``/``assert_sell_is_own``/타입)뿐이며,
``read_own_attribution``(그 모듈의 DB 리더)은 호출하지 않는다. AST 가드가 그
호출과 ``KISMockOrderLedger``/``kis_mock`` 참조를 격추한다.

증거를 둘로 쪼갠 이유 — kiwoom 은 correlation 을 받지 않는다
--------------------------------------------------------------

``kt10000`` 의 요청 body 는 ``dmst_stex_tp``/``stk_cd``/``ord_qty``/``ord_uv``/
``trde_tp`` 다섯 필드뿐이다. 클라이언트가 붙일 수 있는 식별자가 **없고**, 응답이
돌려주는 것은 브로커가 부여한 ``ord_no`` 다. 따라서 US 랩(alpaca)처럼 브로커
행에서 자기 correlation 을 읽어 내는 방식이 성립하지 않는다. 대신 역할을 쪼갠다:

* **저널**(:class:`OwnOrderJournal`) = 「이 ``ord_no`` 는 우리가 낸 것이다」.
  제출 직후 append-only 로 기록한다. 우리가 저자인 사실(주문번호·side·요청수량)만
  담고, 체결 여부는 담지 않는다.
* **브로커**(``kt00007``) = 「그 주문이 실제로 얼마나 체결됐는가」. 수량·가격의
  진실은 끝까지 브로커다.

저널이 없거나 읽히지 않으면 :class:`~scripts.b0x.kr.attribution.
AttributionUnreadable` 이다 — 「자기 것 없음」이 아니라 「알 수 없음」이고, 그
상태에서 소비자는 자기 포지션 0(매도 파생 없음) + §4 상한 입력 = 계좌 전체로
양쪽을 닫는다. 저널 유실의 방향은 **과소 귀속**이므로 안전하다.

비대칭 (#1835 과 동일 방향)
-----------------------------

* **매수(+)** 는 브로커가 확인한 **체결 수량**만 더한다. 접수/미체결은 아직
  우리 수량이 아니다 → 과소.
* **매도(−)** 는 저널의 매도 **요청 수량 전부**를 상태 무관하게 뺀다. 체결을
  기다렸다 빼면, 이미 팔린 수량이 우리 것으로 남아 legacy 매도로 이어질 수 있다.
* 마지막으로 :func:`scripts.b0x.kr.attribution.scope_positions` 가 **브로커 보유
  수량으로 상한**을 건다: 저널이 뭐라 하든 계좌에 없는 주식은 우리 것이 아니다.

이 모듈은 파일과 브로커를 **읽기만** 한다(저널 append 제외). 주문 경로 없음.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

from scripts.b0x.kr.attribution import (
    AttributedLot,
    AttributionUnreadable,
    OwnFillAttribution,
    attribution_unreadable,
)

#: Journal file name, one per lane directory (next to ``cycles.jsonl``).
JOURNAL_NAME: Final[str] = "own-orders.jsonl"

#: 🔴 Bounded fan-out: attribution queries ``kt00007`` once per distinct order
#: date in the journal. Beyond this many distinct dates the read is declared
#: unreadable rather than hammering the mock API — the safe direction (자기
#: 포지션 0), and a signal that this lane needs a real ledger before it grows.
MAX_ATTRIBUTION_DATE_QUERIES: Final[int] = 20

ATTRIBUTION_SOURCE: Final[str] = (
    "B0-X own-order journal (ord_no ↔ b0xkw-correlation, append-only) × "
    "kt00007 계좌별주문체결내역상세 체결수량: buy=브로커 확인 체결분만 가산, "
    "sell=저널 요청수량 상태무관 차감, 브로커 보유수량 상한 (§39차 ③). "
    "🔴 review.kis_mock_order_ledger 미사용 — v1.6 ① 예외는 브로커 표면 부재 한정"
)


@dataclass(frozen=True, slots=True)
class OwnOrderRecord:
    """One order this lane authored. Authorship + request, never fill state."""

    at: str
    order_no: str
    correlation_id: str
    symbol: str
    side: str
    price: int
    quantity: int
    #: ``YYYYMMDD`` in **KST** — the trading-day key ``kt00007`` indexes on.
    order_date: str

    def canonical(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "order_no": self.order_no,
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "order_date": self.order_date,
        }


class JournalUnreadable(RuntimeError):
    """The own-order journal exists but could not be parsed."""


_KST = dt.timezone(dt.timedelta(hours=9))


def kst_order_date(at: dt.datetime) -> str:
    """``YYYYMMDD`` in KST — Kiwoom indexes 주문일자 by Korean trading day."""

    return at.astimezone(_KST).strftime("%Y%m%d")


@dataclass(frozen=True, slots=True)
class OwnOrderJournal:
    """Append-only record of ``ord_no`` values this lane authored.

    Deliberately a file, not a DB table: it must survive without the
    application database (which this lane otherwise never touches), and an
    append-only text file is the same evidence discipline
    :mod:`scripts.b0x.ledger` already applies to cycle records. Nothing here
    rewrites or deletes a line.
    """

    path: Path

    @classmethod
    def for_lane(cls, *, root: Path, lane: str) -> OwnOrderJournal:
        return cls(path=Path(root).expanduser() / lane / JOURNAL_NAME)

    def append(self, record: OwnOrderRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.canonical(), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_all(self) -> tuple[OwnOrderRecord, ...]:
        """Return every recorded order, or raise.

        🔴 A missing file is an empty journal (this lane has never traded), which
        is a legitimate readable answer. A file that exists but cannot be parsed
        is **not** — that is :class:`JournalUnreadable`, and it must not be
        collapsed into "no orders", which would silently promote every legacy
        holding's absence-of-evidence into "not ours" and, worse, would make a
        genuinely owned position look legacy right after a corruption.
        """

        if not self.path.exists():
            return ()
        records: list[OwnOrderRecord] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                records.append(
                    OwnOrderRecord(
                        at=str(payload["at"]),
                        order_no=str(payload["order_no"]),
                        correlation_id=str(payload["correlation_id"]),
                        symbol=str(payload["symbol"]),
                        side=str(payload["side"]),
                        price=int(payload["price"]),
                        quantity=int(payload["quantity"]),
                        order_date=str(payload["order_date"]),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — any parse failure is unreadable
            raise JournalUnreadable(
                f"own-order journal at {self.path} is unreadable "
                f"({type(exc).__name__}) — refusing to read a corrupt journal as "
                "'this lane never traded'"
            ) from exc
        return tuple(records)

    def own_order_ids(self) -> frozenset[str]:
        return frozenset(record.order_no for record in self.read_all())


@dataclass(frozen=True, slots=True)
class RealizedPnlInput:
    """The only P&L fact this lane can currently prove without inventing one.

    The Kiwoom mock detail surface provides fills and remaining quantity, but
    not a dedicated net realized-P&L field (including fees).  A zero is safe
    only for the bootstrap case where the complete append-only B0-X journal
    proves that this UTC day has no B0-X order activity at all.  Once activity
    exists, the correct value is unreadable until a dedicated P&L evidence
    source is wired; it must never become a fabricated ``Decimal('0')``.
    """

    value: Decimal | None
    source: str
    reason: str | None = None

    @property
    def readable(self) -> bool:
        return self.value is not None

    def canonical(self) -> dict[str, Any]:
        return {
            "readable": self.readable,
            "value": None if self.value is None else format(self.value, "f"),
            "source": self.source,
            "reason": self.reason,
        }


def realized_pnl_input_today(
    *, journal: OwnOrderJournal, now: dt.datetime
) -> RealizedPnlInput:
    """Return a provable bootstrap zero or an explicit unreadable input."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        records = journal.read_all()
    except Exception as exc:  # noqa: BLE001 — corrupt evidence is not a zero
        return RealizedPnlInput(
            value=None,
            source="own_order_journal",
            reason=f"journal_unreadable:{type(exc).__name__}",
        )

    current_day = now.astimezone(dt.UTC).date()
    for record in records:
        try:
            timestamp = dt.datetime.fromisoformat(record.at.replace("Z", "+00:00"))
        except ValueError:
            return RealizedPnlInput(
                value=None,
                source="own_order_journal",
                reason="journal_timestamp_unreadable",
            )
        if timestamp.tzinfo is None:
            return RealizedPnlInput(
                value=None,
                source="own_order_journal",
                reason="journal_timestamp_timezone_missing",
            )
        if timestamp.astimezone(dt.UTC).date() == current_day:
            return RealizedPnlInput(
                value=None,
                source="own_order_journal",
                reason=(
                    "own_order_activity_exists_today_without_dedicated_"
                    "realized_pnl_source"
                ),
            )
    return RealizedPnlInput(
        value=Decimal("0"),
        source="own_order_journal_no_b0x_activity_today",
    )


class OrderDetailReader(Protocol):
    """``ReadOnlyKiwoomMockAccount.read_order_detail`` — the injection seam."""

    async def __call__(
        self, *, order_date: str | None = None, symbol: str | None = None
    ) -> list[dict[str, Any]]: ...


def build_attribution(
    *,
    journal: tuple[OwnOrderRecord, ...],
    filled_by_order_no: dict[str, int],
) -> OwnFillAttribution:
    """저널 × 브로커 체결수량 → 심볼별 자기 순수량. 순수 함수.

    ``filled_by_order_no`` maps ``ord_no`` → broker-reported filled quantity.
    An order absent from that mapping contributes **zero** on the buy side
    (no fill evidence) and its **full requested quantity** on the sell side
    (the asymmetry — see the module docstring).
    """

    bought: dict[str, Decimal] = {}
    bought_notional: dict[str, Decimal] = {}
    sold: dict[str, Decimal] = {}
    buy_rows: dict[str, int] = {}
    sell_rows: dict[str, int] = {}

    for record in journal:
        symbol = record.symbol.strip()
        if not symbol:
            continue
        side = record.side.strip().lower()
        if side == "sell":
            sold[symbol] = sold.get(symbol, Decimal("0")) + Decimal(record.quantity)
            sell_rows[symbol] = sell_rows.get(symbol, 0) + 1
            continue
        if side != "buy":
            continue
        filled = Decimal(int(filled_by_order_no.get(record.order_no, 0)))
        if filled <= 0:
            continue
        bought[symbol] = bought.get(symbol, Decimal("0")) + filled
        bought_notional[symbol] = bought_notional.get(symbol, Decimal("0")) + (
            filled * Decimal(record.price)
        )
        buy_rows[symbol] = buy_rows.get(symbol, 0) + 1

    lots: list[AttributedLot] = []
    for symbol in sorted(set(bought) | set(sold)):
        gross = bought.get(symbol, Decimal("0"))
        net = gross - sold.get(symbol, Decimal("0"))
        if net < 0:
            net = Decimal("0")
        average = (
            (bought_notional.get(symbol, Decimal("0")) / gross)
            if gross > 0
            else Decimal("0")
        )
        lots.append(
            AttributedLot(
                symbol=symbol,
                quantity=net,
                # 🔴 자기 체결 평균가. 브로커의 매입평균가는 legacy 원가가 섞여
                # 있으므로 쓰지 않는다 (#1835 와 같은 이유).
                average_price=average,
                buy_fill_rows=buy_rows.get(symbol, 0),
                sell_rows=sell_rows.get(symbol, 0),
            )
        )
    return OwnFillAttribution(lots=tuple(lots))


async def read_own_attribution(
    *,
    journal: OwnOrderJournal,
    read_order_detail: OrderDetailReader,
) -> OwnFillAttribution | AttributionUnreadable:
    """자기 귀속 — 저널(소유) × kt00007(체결 진실). 어떤 실패든 tri-state.

    🔴 Never touches ``review.kis_mock_order_ledger``. The evidence chain is
    the local journal plus the kiwoom broker, and nothing else.
    """

    try:
        records = journal.read_all()
    except Exception as exc:  # noqa: BLE001 — unreadable, not empty
        return attribution_unreadable(type(exc).__name__)

    if not records:
        # 🔴 A readable, genuinely empty journal. Distinct from unreadable: it
        # means this lane has authored nothing, so *every* holding is legacy —
        # which is exactly right on a coexisting KR-B1 account.
        return OwnFillAttribution(lots=())

    order_dates = sorted({record.order_date for record in records if record.order_date})
    if not order_dates or len(order_dates) > MAX_ATTRIBUTION_DATE_QUERIES:
        return attribution_unreadable(
            "journal_date_fanout_exceeded"
            if order_dates
            else "journal_rows_missing_order_date"
        )

    filled_by_order_no: dict[str, int] = {}
    try:
        for order_date in order_dates:
            for row in await read_order_detail(order_date=order_date):
                order_id = str(row.get("order_id") or "")
                if not order_id:
                    continue
                filled_by_order_no[order_id] = max(
                    filled_by_order_no.get(order_id, 0),
                    int(row.get("filled_quantity") or 0),
                )
    except Exception as exc:  # noqa: BLE001 — any failure is "unknown"
        return attribution_unreadable(type(exc).__name__)

    return build_attribution(journal=records, filled_by_order_no=filled_by_order_no)


__all__ = [
    "ATTRIBUTION_SOURCE",
    "JOURNAL_NAME",
    "MAX_ATTRIBUTION_DATE_QUERIES",
    "JournalUnreadable",
    "OrderDetailReader",
    "OwnOrderJournal",
    "OwnOrderRecord",
    "RealizedPnlInput",
    "build_attribution",
    "kst_order_date",
    "read_own_attribution",
    "realized_pnl_input_today",
]
