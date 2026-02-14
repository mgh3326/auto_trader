"""
Screenshot Holdings Service

파싱된 증권 앱 스크린샷 데이터 → 심볼 해석 → DB 업데이트
"""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import ManualHolding, MarketType
from app.services.broker_account_service import BrokerAccountService
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.stock_alias_service import StockAliasService
from data.coins_info import get_or_refresh_maps
from data.stocks_info import (
    get_kosdaq_name_to_code,
    get_kospi_name_to_code,
    get_us_stocks_data,
)

logger = logging.getLogger(__name__)


class ScreenshotHoldingsService:
    """스크린샷 기반 보유 현황 업데이트 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        """Convert numeric input to Decimal for consistent precision."""
        return Decimal(str(value))

    async def _resolve_symbol(
        self,
        stock_name: str,
        market_section: str,
        broker: str,
    ) -> tuple[str, str, str]:
        """종목명 → 티커 해석 (3단계 fallback)

        Returns:
            (ticker, market_type, resolution_method)
            - ticker: 해석된 티커
            - market_type: "KR" | "US" | "CRYPTO"
            - resolution_method:
              "alias" | "krx_master" | "us_master" | "crypto_name_kr" | "fallback"
        """
        market_section = market_section.lower()
        if market_section == "kr":
            market_type = MarketType.KR
        elif market_section == "crypto":
            market_type = MarketType.CRYPTO
        else:
            market_type = MarketType.US

        # 1단계: StockAlias DB에서 검색
        alias_service = StockAliasService(self.db)
        ticker = await alias_service.get_ticker_by_alias(stock_name, market_type)
        if ticker:
            return ticker, market_type.value, "alias"

        # 2단계: 마스터 데이터 검색
        if market_type == MarketType.KR:
            kospi_map = get_kospi_name_to_code()
            ticker = kospi_map.get(stock_name)
            if ticker:
                return ticker, market_type.value, "krx_master"

            kosdaq_map = get_kosdaq_name_to_code()
            ticker = kosdaq_map.get(stock_name)
            if ticker:
                return ticker, market_type.value, "krx_master"
        elif market_type == MarketType.US:
            us_data = get_us_stocks_data()
            ticker = us_data.get("name_to_symbol", {}).get(stock_name)
            if ticker:
                from app.core.symbol import to_db_symbol

                return to_db_symbol(ticker), market_type.value, "us_master"
        elif market_type == MarketType.CRYPTO:
            try:
                crypto_maps = await get_or_refresh_maps()
                name_to_pair = crypto_maps.get("NAME_TO_PAIR_KR", {})
                ticker = name_to_pair.get(stock_name)
                if ticker:
                    return ticker, market_type.value, "crypto_name_kr"

                coin_to_name = crypto_maps.get("COIN_TO_NAME_KR", {})
                for coin_symbol, kr_name in coin_to_name.items():
                    if kr_name == stock_name:
                        pair = f"KRW-{coin_symbol}"
                        return pair, market_type.value, "crypto_name_kr"
            except Exception as e:
                logger.warning(f"Crypto map lookup failed: {e}")

        # 3단계: Fallback - 이름 그대로 대문자로 반환
        logger.warning(
            f"Symbol not found in alias/master data: {stock_name} ({broker}), "
            "using uppercase name as ticker"
        )
        return stock_name.upper(), market_type.value, "fallback"

    async def _calculate_avg_buy_price(
        self,
        eval_amount: float,
        profit_loss: float,
        quantity: float,
    ) -> float:
        """평가액과 손익으로 평균매입가 역산

        Formula: avg_buy_price = (eval_amount - profit_loss) / quantity
        """
        if quantity <= 0:
            return 0.0
        return (eval_amount - profit_loss) / quantity

    async def resolve_and_update(
        self,
        user_id: int,
        holdings_data: list[dict[str, Any]],
        broker: str,
        account_name: str = "기본 계좌",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """파싱된 보유 종목 데이터 → 심볼 해석 → DB upsert/remove

        Args:
            user_id: 사용자 ID
            holdings_data: 파싱된 보유 종목 데이터 리스트
                [
                    {
                        "stock_name": "이더리움",  # 선택 (symbol이 있으면 생략 가능)
                        "symbol": "KRW-ETH",      # 선택 (있으면 직접 사용, 우선순위)
                        "quantity": 1,
                        "eval_amount": 3000000,
                        "profit_loss": 100000,
                        "profit_rate": 3.4,
                        "avg_buy_price": 2900000,  # 선택 (있으면 직접 사용)
                        "market_section": "crypto",  # 필수: kr|us|crypto
                        "action": "upsert"  # 또는 "remove"
                    },
                    ...
                ]
            broker: 브로커 타입 ("toss", "samsung" 등)
            account_name: 계좌 이름 ("기본 계좌", "퇴직연금", "ISA")
            dry_run: True면 DB 업데이트하지 않고 미리보기만 반환

        Returns:
            {
                "success": True,
                "dry_run": bool,
                "message": str,
                "broker": str,
                "account_name": str,
                "parsed_count": int,
                "holdings": [
                    {
                        "stock_name": str,
                        "resolved_ticker": str,
                        "market_type": str,
                        "quantity": float,
                        "avg_buy_price": float,
                        "eval_amount": float,
                        "profit_loss": float,
                        "profit_rate": float,
                        "resolution_method": str,
                        "action": str,
                    },
                    ...
                ],
                "warnings": [...],
                # The following fields are ONLY included when dry_run=False:
                # "added_count": int,
                # "updated_count": int,
                # "removed_count": int,
                # "unchanged_count": int,
                # "diff": [...],
            }
        """
        warnings: list[str] = []
        processed_holdings: list[dict[str, Any]] = []

        # 브로커 계좌 조회/생성
        broker_account_service = BrokerAccountService(self.db)
        broker_account = await broker_account_service.get_account_by_user_and_broker(
            user_id=user_id,
            broker_type=broker,  # type: ignore[arg-type]  # String으로 변경됨
            account_name=account_name,
        )

        if not broker_account:
            if dry_run:
                warnings.append(
                    f"Broker account would be created: {broker}/{account_name}"
                )
                # dry_run 시에는 가상 계정 ID 사용
                broker_account_id = 0
            else:
                broker_account = await broker_account_service.create_account(
                    user_id=user_id,
                    broker_type=broker,  # type: ignore[arg-type]
                    account_name=account_name,
                )
                broker_account_id = broker_account.id
        else:
            broker_account_id = broker_account.id

        # 기존 보유 종목 조회 (diff 계산용)
        existing_holdings = await self.db.execute(
            select(ManualHolding).where(
                ManualHolding.broker_account_id == broker_account_id
            )
        )
        old_map = {
            (h.ticker, h.market_type.value): h
            for h in existing_holdings.scalars().all()
        }

        # 각 종목 처리
        diff: list[dict[str, Any]] = []
        added_count = 0
        updated_count = 0
        removed_count = 0
        unchanged_count = 0

        manual_holdings_service = ManualHoldingsService(self.db)

        for holding_data in holdings_data:
            stock_name = holding_data.get("stock_name", "").strip()
            symbol = holding_data.get("symbol", "").strip().upper()
            market_section_raw = holding_data.get("market_section", "")
            market_section = (
                market_section_raw.lower().strip() if market_section_raw else ""
            )
            action = holding_data.get("action", "upsert").lower()

            if market_section not in ("kr", "us", "crypto"):
                warnings.append(
                    f"Skipping holding: invalid or missing market_section '{market_section_raw}' "
                    f"(must be kr|us|crypto)"
                )
                continue

            if not stock_name and not symbol:
                warnings.append(
                    "Skipping holding: both stock_name and symbol are empty"
                )
                continue

            if market_section == "kr":
                market_type = MarketType.KR
            elif market_section == "crypto":
                market_type = MarketType.CRYPTO
            else:
                market_type = MarketType.US

            if action == "remove":
                if symbol:
                    ticker = symbol
                else:
                    ticker, _, _ = await self._resolve_symbol(
                        stock_name, market_section, broker
                    )

                existing = old_map.get((ticker, market_type.value))
                if existing:
                    if not dry_run:
                        await manual_holdings_service.delete_holding(existing.id)
                        removed_count += 1
                    diff.append(
                        {
                            "action": "removed",
                            "ticker": ticker,
                            "market_type": market_type.value,
                        }
                    )
                else:
                    warnings.append(
                        f"Cannot remove: {symbol or stock_name} not found in holdings"
                    )
                continue

            quantity = float(holding_data.get("quantity", 0))
            eval_amount = float(holding_data.get("eval_amount", 0))
            profit_loss = float(holding_data.get("profit_loss", 0))
            profit_rate = float(holding_data.get("profit_rate", 0))
            input_avg_buy_price = float(holding_data.get("avg_buy_price", 0) or 0)

            if symbol:
                ticker = symbol
                resolution_method = "direct"
            else:
                ticker, _, resolution_method = await self._resolve_symbol(
                    stock_name, market_section, broker
                )

            if input_avg_buy_price > 0:
                avg_buy_price = input_avg_buy_price
            else:
                avg_buy_price = await self._calculate_avg_buy_price(
                    eval_amount, profit_loss, quantity
                )

            display_name = stock_name if stock_name else symbol

            processed_holdings.append(
                {
                    "stock_name": display_name,
                    "resolved_ticker": ticker,
                    "market_type": market_type.value,
                    "quantity": quantity,
                    "avg_buy_price": round(avg_buy_price, 2),
                    "eval_amount": eval_amount,
                    "profit_loss": profit_loss,
                    "profit_rate": profit_rate,
                    "resolution_method": resolution_method,
                    "action": "upsert",
                }
            )

            if not dry_run:
                existing = old_map.get((ticker, market_type.value))

                if existing:
                    old_qty = float(existing.quantity)
                    old_avg = float(existing.avg_price)

                    if (
                        abs(old_qty - quantity) > 0.0001
                        or abs(old_avg - avg_buy_price) > 0.01
                    ):
                        await manual_holdings_service.update_holding(
                            existing.id,
                            quantity=self._to_decimal(quantity),
                            avg_price=self._to_decimal(avg_buy_price),
                        )
                        updated_count += 1
                        diff.append(
                            {
                                "action": "updated",
                                "ticker": ticker,
                                "market_type": market_type.value,
                                "old_quantity": old_qty,
                                "new_quantity": quantity,
                                "old_avg_price": old_avg,
                                "new_avg_price": avg_buy_price,
                            }
                        )
                    else:
                        unchanged_count += 1
                else:
                    await manual_holdings_service.create_holding(
                        broker_account_id=broker_account_id,
                        ticker=ticker,
                        market_type=market_type,
                        quantity=self._to_decimal(quantity),
                        avg_price=self._to_decimal(avg_buy_price),
                        display_name=display_name,
                    )
                    added_count += 1
                    diff.append(
                        {
                            "action": "added",
                            "ticker": ticker,
                            "market_type": market_type.value,
                            "quantity": quantity,
                            "avg_buy_price": avg_buy_price,
                        }
                    )
            else:
                existing = old_map.get((ticker, market_type.value))
                if existing:
                    old_qty = float(existing.quantity)
                    old_avg = float(existing.avg_price)

                    if (
                        abs(old_qty - quantity) > 0.0001
                        or abs(old_avg - avg_buy_price) > 0.01
                    ):
                        diff.append(
                            {
                                "action": "updated",
                                "ticker": ticker,
                                "market_type": market_type.value,
                                "old_quantity": old_qty,
                                "new_quantity": quantity,
                                "old_avg_price": old_avg,
                                "new_avg_price": avg_buy_price,
                            }
                        )
                    else:
                        unchanged_count += 1
                else:
                    diff.append(
                        {
                            "action": "added",
                            "ticker": ticker,
                            "market_type": market_type.value,
                            "quantity": quantity,
                            "avg_buy_price": avg_buy_price,
                        }
                    )

        # 응답 구성
        result: dict[str, Any] = {
            "success": True,
            "dry_run": dry_run,
            "message": "Preview only (set dry_run=False to update DB)"
            if dry_run
            else "Holdings updated successfully",
            "broker": broker,
            "account_name": account_name,
            "parsed_count": len(processed_holdings),
            "holdings": processed_holdings,
            "warnings": warnings,
        }

        if not dry_run:
            result.update(
                {
                    "added_count": added_count,
                    "updated_count": updated_count,
                    "removed_count": removed_count,
                    "unchanged_count": unchanged_count,
                    "diff": diff,
                }
            )

        return result
