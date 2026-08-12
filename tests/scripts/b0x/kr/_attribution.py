"""Fake 자기 원장 귀속 readers for the KR lane (§36차 2항).

None of these touch a database. They exist for the same reason
``_pending.py`` does: ``read_own_attribution`` converts *any* failure into
:class:`~scripts.b0x.kr.attribution.AttributionUnreadable`, so a test that
forgot to inject would not error — it would quietly take the fail-closed
branch (자기 포지션 0 · §4 상한 = 계좌 전체) and keep passing while proving
nothing about the readable path.
"""

from __future__ import annotations

from decimal import Decimal

from scripts.b0x.kr import attribution as kr_attribution


def attributed(**quantities: object):
    """A reader whose ledger attributes exactly ``symbol=quantity`` to B0-X.

    ``quantity`` may be a plain int/str; ``average_price`` defaults to a
    single fill at 1 KRW unless given as a ``(quantity, average_price)`` pair.
    """

    lots: list[kr_attribution.AttributedLot] = []
    for symbol, value in quantities.items():
        if isinstance(value, tuple):
            quantity, average_price = value
        else:
            quantity, average_price = value, 1
        lots.append(
            kr_attribution.AttributedLot(
                symbol=symbol,
                quantity=Decimal(str(quantity)),
                average_price=Decimal(str(average_price)),
                buy_fill_rows=1,
                sell_rows=0,
            )
        )
    resolved = kr_attribution.OwnFillAttribution(lots=tuple(lots))

    async def _read(
        *, correlation_prefix: str
    ) -> kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable:
        assert correlation_prefix == "b0xk-", (
            f"cycle asked with prefix {correlation_prefix!r}, expected 'b0xk-'"
        )
        return resolved

    return _read


def no_attribution():
    """원장이 답했고, 이 레인 소유는 하나도 없다 — legacy-only 계좌.

    🔴 「읽지 못했다」와 구분된다: 이것은 **readable** 이므로 preflight 를
    통과해야 하고(§36차 2항, mutant ④), 그런데도 매도/물타기 후보는 0 이어야
    한다(mutant ①②).
    """

    return attributed()


def unreadable_attribution(cause: str = "RuntimeError"):
    """귀속 원장이 답하지 못한 상태 — 양방향 fail-closed 를 증명하는 입력."""

    resolved = kr_attribution.attribution_unreadable(cause)

    async def _read(
        *, correlation_prefix: str
    ) -> kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable:
        del correlation_prefix
        return resolved

    return _read


def exploding_attribution(exc: Exception):
    """A reader that raises — the cycle itself must convert it, not the test."""

    async def _read(
        *, correlation_prefix: str
    ) -> kr_attribution.OwnFillAttribution | kr_attribution.AttributionUnreadable:
        del correlation_prefix
        raise exc

    return _read


__all__ = [
    "attributed",
    "exploding_attribution",
    "no_attribution",
    "unreadable_attribution",
]
