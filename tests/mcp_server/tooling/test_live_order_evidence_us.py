from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest


def _row(**kw):
    base = {"odno": "US-1", "pdno": "AAPL", "ovrs_excg_cd": "NASD"}
    base.update(kw)
    return base


@pytest.mark.unit
def test_normalize_overseas_row_maps_ft_keys():
    from app.mcp_server.tooling.live_order_evidence import (
        _normalize_overseas_for_classify,
    )

    norm = _normalize_overseas_for_classify(
        _row(ft_ord_qty="3", ft_ccld_qty="3", ft_ccld_unpr3="191.5")
    )
    assert norm["odno"] == "US-1"
    assert norm["ord_qty"] == "3"
    assert norm["tot_ccld_qty"] == "3"
    assert norm["ccld_unpr"] == "191.5"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_filled():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-1"

    fake_kis = object()
    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev,
            "_build_us_exchange_candidates",
            new=AsyncMock(return_value=["NASD"]),
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(
                return_value=(
                    _row(ft_ord_qty="3", ft_ccld_qty="3", ft_ccld_unpr3="191.5"),
                    "NASD",
                )
            ),
        ),
    ):
        adapter = ev.UsOverseasEvidenceAdapter()
        evidence = await adapter.fetch_evidence(_Row())
    assert evidence.verdict == FillVerdict.FILLED
    assert evidence.filled_qty == Decimal("3")
    assert evidence.avg_price == Decimal("191.5")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_not_found_is_pending():
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-MISSING"

    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())
    assert evidence.verdict == FillVerdict.PENDING  # fail-closed, no booking


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_no", "symbol", "ordered_price"),
    [
        ("0031116724", "GOOGL", "380.1"),
        ("0031117219", "AMZN", "262"),
    ],
)
async def test_us_adapter_classifies_expired_day_order_from_broker_terminal_shape(
    order_no: str, symbol: str, ordered_price: str
) -> None:
    """ROB-952: KIS overseas DAY expiry must not become a phantom pending order."""
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        exchange = "NASD"

        def __init__(self, order_no: str, symbol: str) -> None:
            self.order_no = order_no
            self.symbol = symbol

    broker_order = _row(
        odno=order_no,
        pdno=symbol,
        ft_ord_qty="1",
        ft_ccld_qty="0",
        nccs_qty="0",
        ft_ord_unpr3=ordered_price,
        ord_dt="20260716",
        ord_tmd="224201",
    )
    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(return_value=(broker_order, "NASD")),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(
            _Row(order_no, symbol)
        )

    assert evidence.verdict == FillVerdict.EXPIRED
    assert evidence.filled_qty == Decimal("0")
    assert evidence.reason_code == "expired"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_classifies_explicit_cancel_evidence_as_cancelled() -> None:
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "AAPL"
        exchange = "NASD"
        order_no = "US-CANCELLED-1"

    broker_order = _row(
        odno="US-CANCELLED-1",
        ft_ord_qty="1",
        ft_ccld_qty="0",
        nccs_qty="0",
        rvse_cncl_dvsn_name="취소",
    )
    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(return_value=(broker_order, "NASD")),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())

    assert evidence.verdict == FillVerdict.CANCELLED
    assert evidence.reason_code == "cancelled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_keeps_actual_open_order_pending() -> None:
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "GOOGL"
        exchange = "NASD"
        order_no = "US-OPEN-1"

    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(
                return_value=(
                    _row(
                        odno="US-OPEN-1",
                        pdno="GOOGL",
                        ft_ord_qty="1",
                        ft_ccld_qty="0",
                        nccs_qty="1",
                    ),
                    "NASD",
                )
            ),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())

    assert evidence.verdict == FillVerdict.PENDING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_anchors_history_window_on_row_order_date() -> None:
    """ROB-719 gap A: the TTTS3035R inquiry is anchored on the row's order
    date, not a fixed now-7d window.

    The Step3 probe showed KIS retains ~90 days of overseas order history
    (16/16 stuck GOOGL orders back to 07-17 visible); the adapter must
    therefore probe ``order_date-1 .. today`` (the -1d absorbs broker
    ``ord_dt`` vs UTC ``trade_date`` skew) so a weeks-old order still
    produces evidence.  A row with no derivable date falls back to the
    recent 7-day window — strictly narrower, fail-closed.
    """
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.mcp_server.tooling import live_order_evidence as ev

    captured: list[dict] = []

    async def _fake_history(**kwargs):
        captured.append(kwargs)
        return [
            _row(
                odno="US-OLD-1",
                pdno="GOOGL",
                ft_ord_qty="1",
                ft_ccld_qty="1",
                ft_ccld_unpr3="170.0",
            )
        ]

    fake_kis = SimpleNamespace(
        inquire_daily_order_overseas=AsyncMock(side_effect=_fake_history)
    )

    class _Row:
        symbol = "GOOGL"
        exchange = "NASD"
        order_no = "US-OLD-1"

        def __init__(self, trade_date):
            self.trade_date = trade_date

    order_dt = datetime.now(UTC) - timedelta(days=70)
    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
    ):
        await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row(order_dt))

    assert len(captured) == 1
    assert captured[0]["start_date"] == (order_dt.date() - timedelta(days=1)).strftime(
        "%Y%m%d"
    )
    assert captured[0]["end_date"] == datetime.now().strftime("%Y%m%d")

    # A row older than the documented depth clamps the window start at
    # today-89d — it is probed but cannot match, staying fail-closed.
    captured.clear()
    ancient_dt = datetime.now(UTC) - timedelta(days=120)
    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
    ):
        await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row(ancient_dt))

    assert captured[0]["start_date"] == (datetime.now() - timedelta(days=89)).strftime(
        "%Y%m%d"
    )

    # No derivable order date → the narrow recent window (unchanged callers).
    captured.clear()

    class _RowNoDate:
        symbol = "GOOGL"
        exchange = "NASD"
        order_no = "US-OLD-1"
        trade_date = None
        created_at = None

    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
    ):
        await ev.UsOverseasEvidenceAdapter().fetch_evidence(_RowNoDate())

    assert captured[0]["start_date"] == (datetime.now() - timedelta(days=7)).strftime(
        "%Y%m%d"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_adapter_preserves_partial_fill_when_expired_row_has_fill_evidence() -> (
    None
):
    from app.mcp_server.tooling import live_order_evidence as ev
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        symbol = "GOOGL"
        exchange = "NASD"
        order_no = "US-PARTIAL-EXPIRED"

    with (
        patch.object(ev, "_create_live_kis_client", return_value=object()),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
        patch.object(
            ev,
            "_find_us_order_in_recent_history",
            new=AsyncMock(
                return_value=(
                    _row(
                        odno="US-PARTIAL-EXPIRED",
                        pdno="GOOGL",
                        ft_ord_qty="2",
                        ft_ccld_qty="1",
                        nccs_qty="0",
                        ft_ccld_unpr3="380.1",
                    ),
                    "NASD",
                )
            ),
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())

    assert evidence.verdict == FillVerdict.PARTIAL
    assert evidence.filled_qty == Decimal("1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_reconcile_keeps_row_open_when_history_reports_incomplete() -> None:
    """hk 5342: 불완전 이력 raise는 원장 행을 닫지 않는다.

    ``inquire_daily_order_overseas`` 는 재시도 소진 뒤 부분 목록을 돌려주는 대신
    RuntimeError 를 던진다. 이 테스트는 그 raise 가 evidence 어댑터를 지나
    reconcile 커널에서 종결 전이(marked_expired/marked_cancelled/booked)가 아니라
    ``noop_pending`` 으로 끝나고 행이 열린 채 남는 것을 고정한다.
    """
    from types import SimpleNamespace

    from app.mcp_server.tooling import live_order_evidence as ev
    from app.mcp_server.tooling import live_order_ledger as ll
    from app.services.brokers.kis.mock_scalping_exec.fill_evidence import FillVerdict

    class _Row:
        id = 501
        broker = "kis"
        market = "us"
        symbol = "GOOGL"
        exchange = "NASD"
        order_no = "US-INCOMPLETE-1"
        trade_date = None
        created_at = None

    fake_kis = SimpleNamespace(
        inquire_daily_order_overseas=AsyncMock(
            side_effect=RuntimeError(
                "KIS overseas daily order history incomplete: retryable errors "
                "exhausted at page=2 (accumulated=20, msg_cd=SYDB0050)"
            )
        )
    )
    with (
        patch.object(ev, "_create_live_kis_client", return_value=fake_kis),
        patch.object(
            ev, "_build_us_exchange_candidates", new=AsyncMock(return_value=["NASD"])
        ),
    ):
        evidence = await ev.UsOverseasEvidenceAdapter().fetch_evidence(_Row())

    assert evidence.verdict == FillVerdict.PENDING
    assert evidence.reason_code == "not_found"

    with patch.object(
        ll, "get_evidence_adapter", return_value=ev.UsOverseasEvidenceAdapter()
    ):
        decision = await ll._reconcile_one_live_row(_Row(), dry_run=False)

    # 상태/lifecycle 전이 없음 — 행은 open 으로 남아 다음 scan 대상이다.
    assert decision["action"] == "noop_pending"
    assert decision["reason_code"] == "not_found"
    assert decision["requires_manual_review"] is True
