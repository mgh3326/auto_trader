from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


async def _noop_sleep(_delay: float) -> None:
    return None


@dataclass(slots=True)
class AutomationScenario:
    client_factory: Any
    manual_service_factory: Any
    buy_handler: Any
    sell_handler: Any
    call_kwargs: dict[str, Any] = field(default_factory=dict)

    def apply(self, monkeypatch: Any, kis_tasks: Any) -> ExitStack:
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(kis_tasks, "KISClient", self.client_factory)
        monkeypatch.setattr(
            kis_tasks,
            "_domestic_buy",
            self.buy_handler,
        )
        monkeypatch.setattr(
            kis_tasks,
            "_domestic_sell",
            self.sell_handler,
        )
        monkeypatch.setattr(kis_tasks.asyncio, "sleep", _noop_sleep)

        stack = ExitStack()
        stack.enter_context(
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session)
        )
        stack.enter_context(
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                self.manual_service_factory,
            )
        )
        return stack


def build_domestic_holdings_scenario() -> AutomationScenario:
    class DummyAnalyzer:
        async def analyze_stock_json(self, name: str) -> tuple[dict[str, Any], str]:
            return {"decision": "hold", "confidence": 65}, "gemini-2.5-pro"

        async def close(self) -> None:
            return None

    class DummyKIS:
        async def fetch_my_stocks(self) -> list[dict[str, str]]:
            return [
                {
                    "pdno": "005935",
                    "prdt_name": "삼성전자우",
                    "pchs_avg_pric": "73800",
                    "prpr": "75850",
                    "hldg_qty": "5",
                }
            ]

        async def inquire_korea_orders(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, str]]:
            return [
                {
                    "pdno": "005935",
                    "ord_no": "ORDER001",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "1",
                    "ord_unpr": "73000",
                    "ord_gno_brno": "06010",
                },
                {
                    "pdno": "005935",
                    "ord_no": "ORDER002",
                    "sll_buy_dvsn_cd": "01",
                    "ord_qty": "1",
                    "ord_unpr": "78000",
                    "ord_gno_brno": "06020",
                },
            ]

        async def cancel_korea_order(self, *args: Any, **kwargs: Any) -> dict[str, str]:
            return {"odno": kwargs.get("order_number") or args[0]}

    class EmptyManualService:
        def __init__(self, db: Any):
            self.db = db

        async def get_holdings_by_user(
            self, user_id: int, market_type: Any
        ) -> list[Any]:
            return []

    async def fake_buy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": False,
            "message": "종목 설정 없음 - 매수 건너뜀",
            "orders_placed": 0,
        }

    async def fake_sell(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": False,
            "message": "매도 조건 미충족",
            "orders_placed": 0,
        }

    return AutomationScenario(
        client_factory=DummyKIS,
        manual_service_factory=EmptyManualService,
        buy_handler=fake_buy,
        sell_handler=fake_sell,
        call_kwargs={},
    )


@dataclass(slots=True)
class OverseasNotificationScenario:
    client_factory: Any
    manual_service_factory: Any
    notifier_factory: Any
    buy_handler: Any
    sell_handler: Any
    notifications: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, monkeypatch: Any, kis_tasks: Any) -> ExitStack:
        mock_db_session = MagicMock()
        mock_db_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(kis_tasks, "KISClient", self.client_factory)
        monkeypatch.setattr(
            kis_tasks,
            "_overseas_buy",
            self.buy_handler,
        )
        monkeypatch.setattr(
            kis_tasks,
            "_overseas_sell",
            self.sell_handler,
        )
        monkeypatch.setattr(
            kis_tasks, "get_trade_notifier", lambda: self.notifier_factory()
        )
        monkeypatch.setattr(kis_tasks.asyncio, "sleep", _noop_sleep)

        stack = ExitStack()
        stack.enter_context(
            patch("app.core.db.AsyncSessionLocal", return_value=mock_db_session)
        )
        stack.enter_context(
            patch(
                "app.services.manual_holdings_service.ManualHoldingsService",
                self.manual_service_factory,
            )
        )
        return stack


def build_overseas_notification_scenario(
    *,
    symbol: str,
    name: str,
    avg_price: str,
    current_price: str,
    quantity: str,
    exchange_code: str,
    analysis_decision: str,
    analysis_confidence: int,
    buy_outcome: dict[str, Any] | Exception,
    sell_outcome: dict[str, Any] | Exception,
) -> OverseasNotificationScenario:
    notifications: list[dict[str, Any]] = []

    class DummyAnalyzer:
        async def analyze_stock_json(self, _symbol: str) -> tuple[dict[str, Any], str]:
            return {
                "decision": analysis_decision,
                "confidence": analysis_confidence,
            }, "gemini-2.5-pro"

        async def close(self) -> None:
            return None

    class DummyKIS:
        async def fetch_my_overseas_stocks(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, str]]:
            return [
                {
                    "ovrs_pdno": symbol,
                    "ovrs_item_name": name,
                    "pchs_avg_pric": avg_price,
                    "now_pric2": current_price,
                    "ovrs_cblc_qty": quantity,
                    "ovrs_excg_cd": exchange_code,
                }
            ]

        async def inquire_overseas_orders(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, str]]:
            return []

        async def cancel_overseas_order(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, str]:
            return {"odno": "0000001"}

    class EmptyManualService:
        def __init__(self, db: Any):
            self.db = db

        async def get_holdings_by_user(
            self, user_id: int, market_type: Any
        ) -> list[Any]:
            return []

    class RecordingNotifier:
        async def notify_buy_order(self, **kwargs: Any) -> bool:
            notifications.append({"type": "buy_order", **kwargs})
            return True

        async def notify_sell_order(self, **kwargs: Any) -> bool:
            notifications.append({"type": "sell_order", **kwargs})
            return True

        async def notify_trade_failure(self, **kwargs: Any) -> bool:
            notifications.append({"type": "failure", **kwargs})
            return True

    async def fake_buy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if isinstance(buy_outcome, Exception):
            raise buy_outcome
        return buy_outcome

    async def fake_sell(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if isinstance(sell_outcome, Exception):
            raise sell_outcome
        return sell_outcome

    return OverseasNotificationScenario(
        client_factory=DummyKIS,
        manual_service_factory=EmptyManualService,
        notifier_factory=RecordingNotifier,
        buy_handler=fake_buy,
        sell_handler=fake_sell,
        notifications=notifications,
    )
