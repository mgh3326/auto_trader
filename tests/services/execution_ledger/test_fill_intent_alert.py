"""Fill alert intent + reference slippage (hk 2834).

Pure slippage/rendering, both formatters, and the read-only rung lookup. The
lookup is driven through fake session/repository classes; no database is used.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.monitoring.trade_notifier.formatters_discord import (
    format_fill_notification,
)
from app.monitoring.trade_notifier.formatters_telegram import (
    format_fill_notification_telegram,
)
from app.services.execution_ledger import fill_intent_lookup
from app.services.execution_ledger.fill_intent_lookup import (
    approval_time_price,
    build_fill_intent,
    resolve_fill_intent,
)
from app.services.fill_intent import (
    LOOKUP_FAILED,
    NO_MATCH,
    FillIntent,
    compute_slippage,
    render_slippage_value,
    summarize_thesis,
)
from app.services.fill_notification import FillOrder


def _kr_order(*, side: str, price: float, qty: float = 10.0) -> FillOrder:
    return FillOrder(
        symbol="005930",
        side=side,
        filled_price=price,
        filled_qty=qty,
        filled_amount=price * qty,
        filled_at="2026-11-04T10:00:00",
        account="kis",
        order_id="0001234567",
        market_type="kr",
        currency="KRW",
    )


def _matched(
    *,
    reference: str | None = "68000",
    kind: str | None = "limit",
    strategy: str | None = "rsi_reversion",
    position: int | None = 2,
    total: int | None = 3,
) -> FillIntent:
    return FillIntent(
        status="matched",
        strategy=strategy,
        thesis_summary="눌림 후 반등 확인",
        rung_position=position,
        rung_total=total,
        reference_price=Decimal(reference) if reference is not None else None,
        reference_kind=kind,  # type: ignore[arg-type]
    )


def _discord_text(embed: dict[str, Any]) -> str:
    return "\n".join(f"{f['name']}: {f['value']}" for f in embed["fields"])


# ── pure slippage ───────────────────────────────────────────────────────────


@pytest.mark.unit
class TestSlippageMaths:
    def test_buy_below_limit_is_favourable_in_bp_and_total(self) -> None:
        slip = compute_slippage(
            _kr_order(side="bid", price=67_500.0), _matched(reference="68000")
        )
        assert slip is not None
        assert slip.favourable is True
        assert float(slip.bp) == pytest.approx(-73.529, rel=1e-4)
        assert slip.total == Decimal("-5000")  # (67,500 - 68,000) × 10
        assert render_slippage_value(slip, is_usd=False) == (
            "유리 -73.53bp · 총 -5,000원 (체결 67,500원 / 지정가 68,000원)"
        )

    def test_sell_below_limit_is_adverse(self) -> None:
        slip = compute_slippage(
            _kr_order(side="ask", price=67_500.0), _matched(reference="68000")
        )
        assert slip is not None
        assert slip.favourable is False
        assert slip.total == Decimal("-5000")
        assert render_slippage_value(slip, is_usd=False).startswith("불리 -73.53bp")

    def test_sell_above_limit_is_favourable(self) -> None:
        slip = compute_slippage(
            _kr_order(side="ask", price=68_500.0), _matched(reference="68000")
        )
        assert slip is not None and slip.favourable is True
        assert slip.total == Decimal("5000")

    def test_fill_exactly_at_reference_has_no_direction(self) -> None:
        slip = compute_slippage(
            _kr_order(side="bid", price=68_000.0), _matched(reference="68000")
        )
        assert slip is not None and slip.favourable is None
        assert render_slippage_value(slip, is_usd=False).startswith("기준가 일치")

    def test_usd_total_uses_dollar_format(self) -> None:
        order = FillOrder(
            symbol="AAPL",
            side="bid",
            filled_price=190.0,
            filled_qty=5.0,
            filled_amount=950.0,
            filled_at="2026-11-04T10:00:00",
            account="toss",
            market_type="us",
            currency="USD",
        )
        slip = compute_slippage(
            order, _matched(reference="191", kind="limit", strategy=None)
        )
        assert slip is not None
        assert render_slippage_value(slip, is_usd=True).startswith(
            "유리 -52.36bp · 총 -$5.00"
        )

    @pytest.mark.parametrize(
        "intent",
        [
            None,
            NO_MATCH,
            LOOKUP_FAILED,
            _matched(reference=None, kind=None),
        ],
    )
    def test_no_rung_reference_means_no_slippage(
        self, intent: FillIntent | None
    ) -> None:
        assert compute_slippage(_kr_order(side="bid", price=67_500.0), intent) is None


@pytest.mark.unit
def test_thesis_summary_is_one_bounded_line() -> None:
    assert summarize_thesis("  first line \n second") == "first line"
    assert summarize_thesis("   \n  ") is None
    long = "가" * 200
    out = summarize_thesis(long)
    assert out is not None and len(out) == 60 and out.endswith("…")


# ── formatters ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestIntentLineInFormatters:
    def test_discord_limit_buy_renders_intent_and_favourable_slippage(self) -> None:
        embed = format_fill_notification(
            _kr_order(side="bid", price=67_500.0),
            display_name="삼성전자",
            intent=_matched(),
        )
        text = _discord_text(embed)
        assert "의도: rsi_reversion · 눌림 후 반등 확인 · 래더 2/3" in text
        assert "슬리피지: 유리 -73.53bp · 총 -5,000원" in text

    def test_telegram_limit_buy_renders_intent_and_slippage(self) -> None:
        msg = format_fill_notification_telegram(
            _kr_order(side="bid", price=67_500.0),
            display_name="삼성전자",
            intent=_matched(),
        )
        assert "의도: rsi\\_reversion · 눌림 후 반등 확인 · 래더 2/3" in msg
        assert "슬리피지: 유리 -73.53bp · 총 -5,000원" in msg

    @pytest.mark.parametrize(
        "formatter_name",
        ["discord", "telegram"],
    )
    def test_no_match_says_direct_order_and_no_rung_slippage(
        self, formatter_name: str
    ) -> None:
        order = _kr_order(side="ask", price=67_500.0)
        if formatter_name == "discord":
            text = _discord_text(
                format_fill_notification(
                    order, display_name="삼성전자", intent=NO_MATCH
                )
            )
        else:
            text = format_fill_notification_telegram(
                order, display_name="삼성전자", intent=NO_MATCH
            )
        assert "의도: 기록 없음(직접 주문)" in text
        assert "슬리피지" not in text
        assert "vs 주문가" not in text  # no order_price on this fixture

    @pytest.mark.parametrize("formatter_name", ["discord", "telegram"])
    def test_lookup_failed_says_unknown_and_no_slippage(
        self, formatter_name: str
    ) -> None:
        order = _kr_order(side="bid", price=67_500.0)
        if formatter_name == "discord":
            text = _discord_text(
                format_fill_notification(
                    order, display_name="삼성전자", intent=LOOKUP_FAILED
                )
            )
        else:
            text = format_fill_notification_telegram(
                order, display_name="삼성전자", intent=LOOKUP_FAILED
            )
        assert "의도: 조회 실패" in text
        assert "슬리피지" not in text

    def test_no_intent_argument_keeps_the_message_unchanged(self) -> None:
        order = _kr_order(side="bid", price=67_500.0)
        embed = format_fill_notification(order, display_name="삼성전자")
        assert all(field["name"] != "의도" for field in embed["fields"])
        msg = format_fill_notification_telegram(order, display_name="삼성전자")
        assert "의도" not in msg

    def test_telegram_escapes_markdown_control_characters_in_free_text(self) -> None:
        """A raw ``_``/``*`` in a thesis would make Telegram reject the message."""
        intent = FillIntent(
            status="matched",
            strategy="mean_*rev*",
            thesis_summary="breakout_[retest]",
            rung_position=1,
            rung_total=1,
        )
        msg = format_fill_notification_telegram(
            _kr_order(side="bid", price=67_500.0),
            display_name="삼성전자",
            intent=intent,
        )
        assert "의도: mean\\_\\*rev\\* · breakout\\_\\[retest] · 래더 1/1" in msg
        intent_line = next(line for line in msg.splitlines() if "의도:" in line)
        assert intent_line.startswith("의도: ")

    def test_market_order_without_stored_price_has_no_slippage_line(self) -> None:
        intent = FillIntent(
            status="matched",
            strategy="rsi_reversion",
            thesis_summary="x",
            rung_position=1,
            rung_total=1,
            reference_price=None,
            reference_kind=None,
        )
        embed = format_fill_notification(
            _kr_order(side="bid", price=67_500.0),
            display_name="삼성전자",
            intent=intent,
        )
        assert "슬리피지" not in _discord_text(embed)
        assert "의도:" in _discord_text(embed)

    def test_market_order_with_approval_price_uses_that_reference(self) -> None:
        intent = _matched(reference="68100", kind="approval_price")
        text = _discord_text(
            format_fill_notification(
                _kr_order(side="bid", price=67_500.0),
                display_name="삼성전자",
                intent=intent,
            )
        )
        assert "승인 시점가 68,100원" in text
        assert "유리" in text


# ── lookup (read-only, fake session/repository) ─────────────────────────────


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


_KST = timezone(timedelta(hours=9))
_CREATED = datetime(2026, 11, 4, 9, 0, tzinfo=_KST)


def _rung(
    rung_index: int,
    *,
    limit_price: str | None = "68000",
    created_at: datetime = _CREATED,
) -> SimpleNamespace:
    return SimpleNamespace(
        rung_index=rung_index,
        limit_price=Decimal(limit_price) if limit_price is not None else None,
        quantity=Decimal("10"),
        created_at=created_at,
    )


def _proposal(
    *,
    order_type: str = "limit",
    strategy: str | None = "rsi_reversion",
    thesis: str | None = "눌림 후 반등 확인",
    source_asof: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        order_type=order_type,
        strategy=strategy,
        thesis=thesis,
        source_asof=source_asof,
    )


class _RepoMatch:
    """Fake OrderProposalsService: one matching rung, its proposal, and the ladder."""

    rung = _rung(1)
    proposal = _proposal()
    rungs = [_rung(0), _rung(1), _rung(2)]
    calls: list[dict[str, Any]] = []

    def __init__(self, session: object) -> None:
        self._session = session

    async def find_fill_intent_rows(self, **kwargs: Any) -> tuple[Any, Any, list[Any]]:
        type(self).calls.append(kwargs)
        return type(self).proposal, type(self).rung, type(self).rungs


class _RepoNoMatch(_RepoMatch):
    async def find_fill_intent_rows(self, **kwargs: Any) -> None:  # type: ignore[override]
        type(self).calls.append(kwargs)
        return None


class _RepoRaises(_RepoMatch):
    async def find_fill_intent_rows(self, **kwargs: Any) -> None:  # type: ignore[override]
        raise RuntimeError("db unavailable")


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


def _reset(repo: type[_RepoMatch]) -> None:
    repo.calls = []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_matches_rung_and_builds_limit_reference() -> None:
    _reset(_RepoMatch)
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00+09:00",
        account_mode="kis_live",
        session_factory=_fake_session_factory,
        service_cls=_RepoMatch,
    )
    assert intent.status == "matched"
    assert intent.strategy == "rsi_reversion"
    assert intent.thesis_summary == "눌림 후 반등 확인"
    assert (intent.rung_position, intent.rung_total) == (2, 3)
    assert intent.reference_price == Decimal("68000")
    assert intent.reference_kind == "limit"
    assert _RepoMatch.calls == [
        {"broker_order_id": "0001234567", "account_mode": "kis_live"}
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_no_rung_says_no_match() -> None:
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00+09:00",
        session_factory=_fake_session_factory,
        service_cls=_RepoNoMatch,
    )
    assert intent == NO_MATCH


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_exception_becomes_lookup_failed_and_never_raises() -> None:
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00+09:00",
        session_factory=_fake_session_factory,
        service_cls=_RepoRaises,
    )
    assert intent == LOOKUP_FAILED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_broker_order_id_cannot_prove_absence() -> None:
    intent = await resolve_fill_intent(
        broker_order_id=None,
        filled_at="2026-11-04T10:00:00+09:00",
        session_factory=_fake_session_factory,
        service_cls=_RepoNoMatch,
    )
    assert intent == LOOKUP_FAILED


@pytest.mark.unit
def test_market_order_reference_comes_only_from_a_stored_eligible_price() -> None:
    stored = {
        "auto_approved": {
            "eligibility": [
                {"rung_index": 0, "eligible": True, "current_price": "1"},
                {"rung_index": 1, "eligible": False, "current_price": "999"},
                {"rung_index": 1, "eligible": True, "current_price": "68100"},
            ]
        }
    }
    assert approval_time_price(stored, 1) == Decimal("68100")
    # a rejected decision's price is not an approval-time reference
    assert (
        approval_time_price(
            {
                "auto_approved": {
                    "eligibility": [
                        {"rung_index": 1, "eligible": False, "current_price": "999"}
                    ]
                }
            },
            1,
        )
        is None
    )
    # manual approval stores no auto decision at all
    assert approval_time_price({}, 1) is None
    assert approval_time_price(None, 1) is None
    assert approval_time_price({"auto_approved": {"eligibility": "x"}}, 1) is None


@pytest.mark.unit
def test_limit_order_ignores_stored_price_and_market_order_ignores_limit() -> None:
    rungs = [_rung(0), _rung(1)]
    market_with_limit = build_fill_intent(
        _proposal(order_type="market", source_asof=None),
        _rung(1, limit_price="68000"),
        rungs,
    )
    assert market_with_limit.reference_price is None
    limit_with_stored = build_fill_intent(
        _proposal(
            order_type="limit",
            source_asof={
                "auto_approved": {
                    "eligibility": [
                        {"rung_index": 1, "eligible": True, "current_price": "1"}
                    ]
                }
            },
        ),
        _rung(1, limit_price="68000"),
        rungs,
    )
    assert limit_with_stored.reference_price == Decimal("68000")
    assert limit_with_stored.reference_kind == "limit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_fill_notification_renders_lookup_failure_and_still_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alert goes out even when the rung lookup raises."""
    from app.services.execution_ledger.fill_ingest import send_fill_notification

    monkeypatch.setattr(fill_intent_lookup, "AsyncSessionLocal", _fake_session_factory)
    monkeypatch.setattr(fill_intent_lookup, "OrderProposalsService", _RepoRaises)

    notify_fill = AsyncMock(return_value=True)
    notifier = SimpleNamespace(notify_fill=notify_fill)
    enrichment = AsyncMock(return_value=None)

    ok = await send_fill_notification(
        _kr_order(side="bid", price=67_500.0),
        notifier_factory=lambda: notifier,
        enrichment_fetcher=enrichment,
    )

    assert ok is True
    notify_fill.assert_awaited_once()
    assert notify_fill.call_args.kwargs["intent"] == LOOKUP_FAILED
    rendered = format_fill_notification_telegram(
        notify_fill.call_args.args[0],
        display_name="삼성전자",
        intent=notify_fill.call_args.kwargs["intent"],
    )
    assert "의도: 조회 실패" in rendered


class _SlowRepo(_RepoMatch):
    async def find_fill_intent_rows(self, **kwargs: Any) -> None:  # type: ignore[override]
        await asyncio.sleep(5)


class _MismatchedDateRepo(_RepoMatch):
    rung = _rung(1, created_at=datetime(2026, 11, 3, 9, 0, tzinfo=_KST))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lookup_times_out_to_lookup_failed() -> None:
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00+09:00",
        session_factory=_fake_session_factory,
        service_cls=_SlowRepo,
        timeout_seconds=0.05,
    )
    assert intent == LOOKUP_FAILED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rung_from_another_trading_date_is_not_attributed() -> None:
    """A reused KIS order number must not borrow an older day's strategy."""
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00+09:00",
        session_factory=_fake_session_factory,
        service_cls=_MismatchedDateRepo,
    )
    assert intent == LOOKUP_FAILED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_day_rung_is_attributed_with_fill_date() -> None:
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00+09:00",
        session_factory=_fake_session_factory,
        service_cls=_RepoMatch,
    )
    assert intent.status == "matched"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_fill_notification_passes_account_mode_to_the_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mock fill must not attribute the live rung with the same order number."""
    from app.services.execution_ledger.fill_ingest import send_fill_notification

    _reset(_RepoMatch)
    monkeypatch.setattr(fill_intent_lookup, "AsyncSessionLocal", _fake_session_factory)
    monkeypatch.setattr(fill_intent_lookup, "OrderProposalsService", _RepoMatch)

    notify_fill = AsyncMock(return_value=True)
    notifier = SimpleNamespace(notify_fill=notify_fill)
    await send_fill_notification(
        _kr_order(side="bid", price=67_500.0),
        notifier_factory=lambda: notifier,
        enrichment_fetcher=AsyncMock(return_value=None),
        account_mode="mock",
    )

    assert _RepoMatch.calls[-1]["account_mode"] == "mock"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_fill_time_is_read_as_kst_and_still_date_checked() -> None:
    """A naive filled_at is KST; a rung from another KST day is still refused."""
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00",
        session_factory=_fake_session_factory,
        service_cls=_MismatchedDateRepo,
    )
    assert intent == LOOKUP_FAILED

    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at="2026-11-04T10:00:00",
        session_factory=_fake_session_factory,
        service_cls=_RepoMatch,
    )
    assert intent.status == "matched"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("filled_at", [None, "", "not-a-timestamp"])
async def test_unreadable_fill_time_fails_closed_before_any_read(
    filled_at: str | None,
) -> None:
    _reset(_RepoMatch)
    intent = await resolve_fill_intent(
        broker_order_id="0001234567",
        filled_at=filled_at,
        session_factory=_fake_session_factory,
        service_cls=_RepoMatch,
    )
    assert intent == LOOKUP_FAILED
    assert _RepoMatch.calls == []
