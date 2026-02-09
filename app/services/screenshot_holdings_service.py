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

# 마스터 데이터 (lazy loading)
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
            - resolution_method: "alias" | "krx_master" | "us_master" | "fallback"
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
                        "stock_name": "효성중공업",
                        "quantity": 1,
                        "eval_amount": 2230000,
                        "profit_loss": -170000,
                        "profit_rate": -7.0,
                        "market_section": "kr",
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
                "added_count": int,  # dry_run=False일 때만
                "updated_count": int,  # dry_run=False일 때만
                "removed_count": int,  # dry_run=False일 때만
                "unchanged_count": int,  # dry_run=False일 때만
                "diff": [...],  # dry_run=False일 때만
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
            market_section = holding_data.get("market_section", "kr").lower()
            action = holding_data.get("action", "upsert").lower()

            # 필수 필드 검증
            if not stock_name:
                warnings.append("Skipping holding: missing stock_name")
                continue

            if action == "remove":
                # 삭제 요청
                if market_section == "kr":
                    market_type = MarketType.KR
                elif market_section == "crypto":
                    market_type = MarketType.CRYPTO
                else:
                    market_type = MarketType.US
                ticker = (
                    await self._resolve_symbol(stock_name, market_section, broker)
                )[0]

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
                        f"Cannot remove: {stock_name} not found in holdings"
                    )
                continue

            # upsert 요청
            quantity = float(holding_data.get("quantity", 0))
            eval_amount = float(holding_data.get("eval_amount", 0))
            profit_loss = float(holding_data.get("profit_loss", 0))
            profit_rate = float(holding_data.get("profit_rate", 0))

            # 심볼 해석
            ticker, market_type, resolution_method = await self._resolve_symbol(
                stock_name, market_section, broker
            )

            # 평균매입가 계산
            avg_buy_price = await self._calculate_avg_buy_price(
                eval_amount, profit_loss, quantity
            )

            processed_holdings.append(
                {
                    "stock_name": stock_name,
                    "resolved_ticker": ticker,
                    "market_type": market_type,
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
                market_enum = MarketType(market_type)
                existing = old_map.get((ticker, market_type))

                if existing:
                    # 기존 종목 업데이트
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
                                "market_type": market_type,
                                "old_quantity": old_qty,
                                "new_quantity": quantity,
                                "old_avg_price": old_avg,
                                "new_avg_price": avg_buy_price,
                            }
                        )
                    else:
                        unchanged_count += 1
                else:
                    # 신규 종목 추가
                    await manual_holdings_service.create_holding(
                        broker_account_id=broker_account_id,
                        ticker=ticker,
                        market_type=market_enum,
                        quantity=self._to_decimal(quantity),
                        avg_price=self._to_decimal(avg_buy_price),
                        display_name=stock_name,
                    )
                    added_count += 1
                    diff.append(
                        {
                            "action": "added",
                            "ticker": ticker,
                            "market_type": market_type,
                            "quantity": quantity,
                            "avg_buy_price": avg_buy_price,
                        }
                    )
            else:
                # dry_run: 미리보기용 diff 계산
                existing = old_map.get((ticker, market_type))
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
                                "market_type": market_type,
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
                            "market_type": market_type,
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
