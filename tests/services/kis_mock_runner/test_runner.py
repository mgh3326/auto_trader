from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.kis_mock_runner.control import KillMode, KillSwitchState
from app.services.kis_mock_runner.envelope import AccountEnvelopeSnapshot, OrderIntent
from app.services.kis_mock_runner.notifications import DeliveryReport, DiscordNotifier
from app.services.kis_mock_runner.overlay import OverlayBinding, OverlayRequired
from app.services.kis_mock_runner.runner import (
    AttributedPendingEntry,
    KISMockRunner,
    RunnerStatus,
)


class FakeStore:
    def __init__(self, state: KillSwitchState | Exception) -> None:
        self.state = state
        self.writes: list[tuple[KillMode, str, str]] = []

    async def read(self) -> KillSwitchState:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    async def set_mode(
        self, *, mode: KillMode, reason: str, updated_by: str
    ) -> KillSwitchState:
        self.writes.append((mode, reason, updated_by))
        state = KillSwitchState(mode=mode, reason=reason, updated_by=updated_by)
        self.state = state
        return state


class TrackingLease:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> TrackingLease:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.exited += 1


class PendingEntryPort:
    def __init__(self, entries: tuple[AttributedPendingEntry, ...]) -> None:
        self.entries = entries
        self.cancelled: list[str] = []

    async def list_attributed_pending_entries(
        self,
    ) -> tuple[AttributedPendingEntry, ...]:
        return self.entries

    async def cancel_pending_entry(self, entry: AttributedPendingEntry) -> None:
        self.cancelled.append(entry.order_id)


class RecordingNotifier:
    def __init__(self) -> None:
        self.lifecycle_events: list[str] = []
        self.alert_events: list[str] = []

    async def lifecycle(self, *, event: str, payload: dict) -> DeliveryReport:
        del payload
        self.lifecycle_events.append(event)
        return DeliveryReport(channel="kr_lifecycle", delivered=True)

    async def alert(self, *, event: str, payload: dict) -> DeliveryReport:
        del payload
        self.alert_events.append(event)
        return DeliveryReport(channel="alerts", delivered=True)


def _active_state() -> KillSwitchState:
    return KillSwitchState(
        mode=KillMode.ACTIVE,
        reason="initial_control_row",
        updated_by="migration:KR-B0",
    )


def _snapshot(**overrides: object) -> AccountEnvelopeSnapshot:
    values: dict[str, object] = {
        "session_start_nlv_krw": Decimal("100000000"),
        "current_nlv_krw": Decimal("100000000"),
        "available_cash_krw": Decimal("10000000"),
        "projected_gross_exposure_krw": Decimal("10000000"),
        "positions_including_pending_reserved": 1,
        "new_entries_this_xkrx_session": 1,
        "planned_exits_this_xkrx_session": 0,
        "cash_is_fresh": True,
        "is_cash_only": True,
        "margin_enabled": False,
        "short_enabled": False,
    }
    values.update(overrides)
    return AccountEnvelopeSnapshot(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_default_disabled_stops_before_lock_db_or_notification() -> None:
    class ExplodingStore:
        async def read(self):
            raise AssertionError("kill store must not be read")

        async def set_mode(self, **kwargs):
            raise AssertionError("kill store must not be written")

    lease = TrackingLease()
    notifier = RecordingNotifier()
    runner = KISMockRunner(
        environment={},
        tag="test",
        kill_switch_store=ExplodingStore(),
        writer_lease=lease,
        notifier=notifier,
    )
    result = await runner.run_once()
    assert result.status is RunnerStatus.DEFAULT_DISABLED
    assert result.broker_calls == 0
    assert lease.entered == 0
    assert notifier.lifecycle_events == []
    assert notifier.alert_events == []


@pytest.mark.asyncio
async def test_empty_overlay_refuses_before_intent_generation() -> None:
    lease = TrackingLease()
    notifier = RecordingNotifier()
    runner = KISMockRunner(
        environment={"KIS_MOCK_RUNNER_ENABLED": "true"},
        tag="test",
        kill_switch_store=FakeStore(_active_state()),
        writer_lease=lease,
        notifier=notifier,
    )
    result = await runner.run_once()
    assert result.status is RunnerStatus.OVERLAY_REQUIRED
    assert result.broker_calls == 0
    assert lease.entered == lease.exited == 1
    assert notifier.lifecycle_events == ["kis_mock_runner_overlay_required"]
    with pytest.raises(OverlayRequired, match="OVERLAY_REQUIRED"):
        await runner.evaluate_overlay_intent(
            intent=OrderIntent(
                side="buy",
                role="entry",
                order_type="limit",
                quantity=Decimal("1"),
                limit_price_krw=Decimal("1000"),
            ),
            snapshot=_snapshot(),
            decision_key="must-not-be-used",
        )


@pytest.mark.asyncio
async def test_control_read_failure_blocks_all_automatic_work() -> None:
    notifier = RecordingNotifier()
    runner = KISMockRunner(
        environment={"KIS_MOCK_RUNNER_ENABLED": "true"},
        tag="test",
        kill_switch_store=FakeStore(RuntimeError("read failure")),
        writer_lease=TrackingLease(),
        notifier=notifier,
    )
    result = await runner.run_once()
    assert result.status is RunnerStatus.GLOBAL_FREEZE
    assert result.kill_switch is not None
    assert result.kill_switch.read_failed is True
    assert result.broker_calls == 0
    assert notifier.alert_events == ["kis_mock_runner_global_freeze"]


@pytest.mark.asyncio
async def test_entry_halt_cancels_only_attributed_pending_entries() -> None:
    pending = PendingEntryPort(
        (
            AttributedPendingEntry(
                order_id="order-1", correlation_id="cid-1", strategy_id="strategy-1"
            ),
        )
    )
    runner = KISMockRunner(
        environment={"KIS_MOCK_RUNNER_ENABLED": "true"},
        tag="test",
        kill_switch_store=FakeStore(
            KillSwitchState(
                mode=KillMode.ENTRY_HALT,
                reason="daily_loss_halt_threshold_reached",
                updated_by="runner",
            )
        ),
        writer_lease=TrackingLease(),
        pending_entry_port=pending,
        notifier=RecordingNotifier(),
    )
    result = await runner.run_once()
    assert result.status is RunnerStatus.ENTRY_HALT
    assert result.canceled_pending_entries == 1
    assert pending.cancelled == ["order-1"]
    assert result.broker_calls == 0


@pytest.mark.asyncio
async def test_entry_halt_allows_exit_through_terminal_envelope_path() -> None:
    store = FakeStore(
        KillSwitchState(
            mode=KillMode.ENTRY_HALT,
            reason="daily_loss_halt_threshold_reached",
            updated_by="kis_mock_runner",
        )
    )
    runner = KISMockRunner(
        environment={"KIS_MOCK_RUNNER_ENABLED": "true"},
        tag="test",
        overlay=OverlayBinding(
            candidate_id="candidate-v1",
            contract_hash="hash-v1",
            strategy_id="strategy-v1",
        ),
        kill_switch_store=store,
        writer_lease=TrackingLease(),
        notifier=RecordingNotifier(),
    )

    result = await runner.evaluate_overlay_intent(
        intent=OrderIntent(
            side="sell",
            role="exit",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price_krw=Decimal("100000"),
        ),
        snapshot=_snapshot(current_nlv_krw=Decimal("97500000")),
        decision_key="2026-08-05:exit-after-halt",
    )

    assert result.status is RunnerStatus.READY_NO_INTENT
    assert result.kill_switch is not None
    assert result.kill_switch.mode is KillMode.ENTRY_HALT
    assert result.envelope is not None and result.envelope.allowed is True
    assert result.correlation_id is not None
    assert store.writes == []
    assert result.broker_calls == 0


@pytest.mark.asyncio
async def test_daily_loss_transitions_durably_to_entry_halt(monkeypatch) -> None:
    from app.services.kis_mock_runner import runner as runner_module

    monkeypatch.setattr(runner_module, "is_krx_regular_session", lambda now: True)
    store = FakeStore(_active_state())
    runner = KISMockRunner(
        environment={"KIS_MOCK_RUNNER_ENABLED": "true"},
        tag="test",
        overlay=OverlayBinding(
            candidate_id="candidate-v1",
            contract_hash="hash-v1",
            strategy_id="strategy-v1",
        ),
        kill_switch_store=store,
        writer_lease=TrackingLease(),
        notifier=RecordingNotifier(),
    )
    result = await runner.evaluate_overlay_intent(
        intent=OrderIntent(
            side="buy",
            role="entry",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price_krw=Decimal("100000"),
        ),
        snapshot=_snapshot(current_nlv_krw=Decimal("97500000")),
        decision_key="2026-08-05:entry-1",
    )
    assert result.status is RunnerStatus.ENVELOPE_BLOCKED
    assert result.envelope is not None and result.envelope.requires_entry_halt
    assert store.writes == [
        (
            KillMode.ENTRY_HALT,
            "daily_loss_halt_threshold_reached",
            "kis_mock_runner",
        )
    ]
    assert result.broker_calls == 0


@pytest.mark.asyncio
async def test_notification_failure_does_not_change_no_submit_result() -> None:
    retries: list[tuple[str, str, str]] = []

    class Recorder:
        async def record(self, *, channel: str, event: str, error_type: str) -> None:
            retries.append((channel, event, error_type))

    async def failing_post(url: str, payload: dict) -> None:
        del url, payload
        raise OSError("synthetic delivery failure")

    notifier = DiscordNotifier(
        {
            "DISCORD_WEBHOOK_KR": "https://invalid.example/kr",
            "DISCORD_WEBHOOK_ALERTS": "https://invalid.example/alerts",
        },
        post=failing_post,
        retry_recorder=Recorder(),
    )
    runner = KISMockRunner(
        environment={"KIS_MOCK_RUNNER_ENABLED": "true"},
        tag="test",
        kill_switch_store=FakeStore(_active_state()),
        writer_lease=TrackingLease(),
        notifier=notifier,
    )
    result = await runner.run_once()
    assert result.status is RunnerStatus.OVERLAY_REQUIRED
    assert result.broker_calls == 0
    assert len(result.notification_reports) == 2
    assert all(report.retry_recorded for report in result.notification_reports)
    assert retries == [
        ("kr_lifecycle", "kis_mock_runner_overlay_required", "OSError"),
        ("alerts", "kis_mock_runner_lifecycle_delivery_failure", "OSError"),
    ]
