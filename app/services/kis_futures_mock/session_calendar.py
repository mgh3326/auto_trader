"""KRX 파생 주간 세션 달력 (hk 5946, 설계 §5·Q12).

🔴 XKRX 주식 달력(`exchange_calendars`)을 재사용하지 않는다. 이 표는 사람이 관리하는
데이터이고 **알 수 없는 날은 거래 불가(fail-closed)** 다:

- ``KNOWN_RANGE`` 밖의 날짜 → ``unknown`` (세션 판정을 시도하지 않는다)
- 범위 안인데 ``SESSION_DAYS`` 에 없는 날 → ``closed`` (휴장)
- 야간 세션(18:00–익일 06:00)은 모델에 없다 (Q7) — 주간 세션 밖의 시각은 전부 닫힘.

시드 범위(2026-11-23 ~ 2026-12-30)는 작성자가 한국 공휴일(2026-12-25 크리스마스)만
반영해 넣었고 **KRX 공지로 확인하지 않았다**. 운영자는 스모크 전에 KRX 공지와 대조하고,
범위를 넓힐 때는 이 파일을 PR 로 고친다(런북). 특별 시간 조정일(수능일·연초 등)은
``SessionDay.close`` 를 따로 적어 표현한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

REGULAR_OPEN = time(8, 45)
REGULAR_CLOSE = time(15, 45)
LAST_TRADE_DAY_CLOSE = time(15, 20)

DayState = Literal["open", "closed", "unknown"]


@dataclass(frozen=True, slots=True)
class SessionDay:
    day: date
    open: time = REGULAR_OPEN
    close: time = REGULAR_CLOSE


KNOWN_RANGE: tuple[date, date] = (date(2026, 11, 23), date(2026, 12, 30))
_HOLIDAYS: frozenset[date] = frozenset({date(2026, 12, 25)})


def _weekday_sessions(start: date, end: date) -> dict[date, SessionDay]:
    days: dict[date, SessionDay] = {}
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in _HOLIDAYS:
            days[cursor] = SessionDay(cursor)
        cursor += timedelta(days=1)
    return days


SESSION_DAYS: dict[date, SessionDay] = _weekday_sessions(*KNOWN_RANGE)


def day_state(day: date) -> DayState:
    start, end = KNOWN_RANGE
    if day < start or day > end:
        return "unknown"
    return "open" if day in SESSION_DAYS else "closed"


def second_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_thursday = first + timedelta(days=(3 - first.weekday()) % 7)
    return first_thursday + timedelta(days=7)


def last_trade_date(contract_month: str) -> date | None:
    """결제월(``YYYY-MM``)의 최종거래일. 달력이 판정하지 못하면 ``None``.

    둘째 목요일이 휴장이면 순차적으로 앞당긴다(KRX 명세). 앞당기는 구간이 달력
    범위 밖이면 추측하지 않고 ``None``.
    """
    year_text, _, month_text = contract_month.partition("-")
    candidate = second_thursday(int(year_text), int(month_text))
    for _ in range(7):
        state = day_state(candidate)
        if state == "unknown":
            return None
        if state == "open":
            return candidate
        candidate -= timedelta(days=1)
    return None


def session_close_for(day: date, contract_month: str) -> time | None:
    """그 날 주문이 열려 있는 마지막 시각. 알 수 없으면 ``None``."""
    state = day_state(day)
    if state != "open":
        return None
    last = last_trade_date(contract_month)
    if last is None:
        return None
    if day == last:
        return LAST_TRADE_DAY_CLOSE
    return SESSION_DAYS[day].close


def is_in_session(now: datetime, contract_month: str) -> bool:
    """``now`` (KST aware) 가 주간 세션 안인가. 판정 불가는 False."""
    close = session_close_for(now.date(), contract_month)
    if close is None:
        return False
    local = now.timetz().replace(tzinfo=None)
    return SESSION_DAYS[now.date()].open <= local < close
