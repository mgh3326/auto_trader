"""보유 자산 관리 서비스

KIS와 Upbit에서 보유 자산 정보를 가져와 user_watch_items 테이블에 저장하고 관리합니다.
"""
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import UserWatchItem, Instrument, InstrumentType, Exchange, BrokerAccount
from app.models.analysis import StockInfo, StockAnalysisResult
from app.services.kis import KISClient
from app.services.upbit import fetch_my_coins


class HoldingsService:
    """보유 자산 관리 서비스"""

    def __init__(self, kis_client: KISClient):
        self.kis_client = kis_client

    async def _get_broker_account(
        self,
        db: AsyncSession,
        user_id: int,
        broker_type: str,
        is_mock: bool = False
    ) -> BrokerAccount:
        """증권사 계정 조회"""
        stmt = select(BrokerAccount).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.broker_type == broker_type,
            BrokerAccount.is_mock == is_mock,
            BrokerAccount.is_active == True
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def fetch_and_update_all_holdings(
        self,
        db: AsyncSession,
        user_id: int = 1,  # 기본 사용자 ID
        is_mock: bool = False
    ) -> Dict[str, Any]:
        """모든 보유 자산(국내주식, 해외주식, 암호화폐)을 가져와 업데이트

        Parameters
        ----------
        db : AsyncSession
            데이터베이스 세션
        user_id : int
            사용자 ID (기본값: 1)
        is_mock : bool
            KIS 모의투자 여부

        Returns
        -------
        Dict[str, Any]
            업데이트 결과 요약
        """
        results = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "kr_stocks": {"count": 0, "items": []},
            "us_stocks": {"count": 0, "items": []},
            "crypto": {"count": 0, "items": []},
            "errors": []
        }

        # Broker 계정 조회
        kis_broker = await self._get_broker_account(db, user_id, "kis", is_mock)
        upbit_broker = await self._get_broker_account(db, user_id, "upbit", False)

        if not kis_broker:
            results["errors"].append(f"KIS 증권사 계정을 찾을 수 없습니다 (모의: {is_mock})")
        if not upbit_broker:
            results["errors"].append("Upbit 증권사 계정을 찾을 수 없습니다")

        # 1. 국내주식 조회 및 업데이트
        try:
            kr_stocks = await self.kis_client.fetch_my_stocks(
                is_mock=is_mock,
                is_overseas=False
            )
            for stock in kr_stocks:
                try:
                    await self._upsert_holding(
                        db=db,
                        user_id=user_id,
                        symbol=stock.get("pdno"),  # 종목코드
                        name=stock.get("prdt_name"),  # 종목명
                        instrument_type=InstrumentType.equity_kr,
                        exchange_code="KRX",
                        quantity=float(stock.get("hldg_qty", 0)),  # 보유수량
                        current_price=float(stock.get("prpr", 0)),  # 현재가
                        broker_account_id=kis_broker.id if kis_broker else None,
                    )
                    results["kr_stocks"]["items"].append({
                        "symbol": stock.get("pdno"),
                        "name": stock.get("prdt_name"),
                        "quantity": float(stock.get("hldg_qty", 0))
                    })
                except Exception as e:
                    await db.rollback()  # 에러 발생 시 롤백
                    results["errors"].append(f"국내주식 {stock.get('pdno')}: {str(e)}")

            results["kr_stocks"]["count"] = len(kr_stocks)
        except Exception as e:
            await db.rollback()  # 에러 발생 시 롤백
            results["errors"].append(f"국내주식 조회 실패: {str(e)}")

        # 2. 미국주식 조회 및 업데이트 (나스닥 + 뉴욕증권거래소)
        try:
            # NASD (나스닥 + NYSE + AMEX 포함)
            us_stocks = await self.kis_client.fetch_my_overseas_stocks(
                is_mock=is_mock,
                exchange_code="NASD",
                currency_code="USD"
            )

            for stock in us_stocks:
                try:
                    # 거래소 코드 결정
                    ovrs_excg_cd = stock.get("ovrs_excg_cd", "NASD")
                    exchange_map = {
                        "NASD": "NASDAQ",
                        "NYSE": "NYSE",
                        "AMEX": "AMEX"
                    }
                    exchange_code = exchange_map.get(ovrs_excg_cd, "NASDAQ")

                    await self._upsert_holding(
                        db=db,
                        user_id=user_id,
                        symbol=stock.get("ovrs_pdno"),  # 해외상품번호
                        name=stock.get("ovrs_item_name"),  # 종목명
                        instrument_type=InstrumentType.equity_us,
                        exchange_code=exchange_code,
                        quantity=float(stock.get("ord_psbl_qty", 0)),  # 주문가능수량
                        current_price=float(stock.get("now_pric2", 0)),  # 현재가
                        broker_account_id=kis_broker.id if kis_broker else None,
                    )
                    results["us_stocks"]["items"].append({
                        "symbol": stock.get("ovrs_pdno"),
                        "name": stock.get("ovrs_item_name"),
                        "quantity": float(stock.get("ord_psbl_qty", 0))
                    })
                except Exception as e:
                    await db.rollback()  # 에러 발생 시 롤백
                    results["errors"].append(f"미국주식 {stock.get('ovrs_pdno')}: {str(e)}")

            results["us_stocks"]["count"] = len(us_stocks)
        except Exception as e:
            await db.rollback()  # 에러 발생 시 롤백
            results["errors"].append(f"미국주식 조회 실패: {str(e)}")

        # 3. 암호화폐 조회 및 업데이트
        try:
            crypto = await fetch_my_coins()

            for coin in crypto:
                try:
                    currency = coin.get("currency")
                    if currency == "KRW":  # 원화는 제외
                        continue

                    # Upbit 마켓 코드 생성 (예: KRW-BTC)
                    market_code = f"KRW-{currency}"

                    await self._upsert_holding(
                        db=db,
                        user_id=user_id,
                        symbol=market_code,
                        name=currency,
                        instrument_type=InstrumentType.crypto,
                        exchange_code="UPBIT",
                        quantity=float(coin.get("balance", 0)),  # 보유수량
                        current_price=float(coin.get("avg_buy_price", 0)),  # 평균매수가
                        broker_account_id=upbit_broker.id if upbit_broker else None,
                    )
                    results["crypto"]["items"].append({
                        "symbol": market_code,
                        "name": currency,
                        "quantity": float(coin.get("balance", 0))
                    })
                except Exception as e:
                    await db.rollback()  # 에러 발생 시 롤백
                    results["errors"].append(f"암호화폐 {coin.get('currency')}: {str(e)}")

            # KRW 제외한 개수
            results["crypto"]["count"] = len([c for c in crypto if c.get("currency") != "KRW"])
        except Exception as e:
            await db.rollback()  # 에러 발생 시 롤백
            results["errors"].append(f"암호화폐 조회 실패: {str(e)}")

        await db.commit()
        return results

    async def _upsert_holding(
        self,
        db: AsyncSession,
        user_id: int,
        symbol: str,
        name: str,
        instrument_type: InstrumentType,
        exchange_code: str,
        quantity: float,
        current_price: float = None,
        broker_account_id: int = None,
    ) -> None:
        """보유 자산을 추가하거나 업데이트

        Parameters
        ----------
        db : AsyncSession
            데이터베이스 세션
        user_id : int
            사용자 ID
        symbol : str
            종목 코드 또는 마켓 코드
        name : str
            종목명
        instrument_type : InstrumentType
            상품 타입
        exchange_code : str
            거래소 코드
        quantity : float
            보유 수량
        current_price : float, optional
            현재가
        broker_account_id : int, optional
            증권사 계정 ID
        """
        # 1. Exchange 확인/생성
        exchange = await self._get_or_create_exchange(db, exchange_code)

        # 2. Instrument 확인/생성
        instrument = await self._get_or_create_instrument(
            db=db,
            exchange_id=exchange.id,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type
        )

        # 3. StockInfo 확인/업데이트 (current_price 저장)
        stock_info = await self._get_or_update_stock_info(
            db=db,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type.value,
            exchange=exchange_code,
            current_price=current_price
        )

        # 4. 최신 분석 결과에서 목표가 가져오기
        analysis = await self._get_latest_analysis(db, stock_info.id if stock_info else None)

        desired_buy_px = None
        target_sell_px = None

        if analysis:
            # 매수 희망 범위 최소값
            desired_buy_px = analysis.buy_hope_min
            # 매도 목표 범위 최대값
            target_sell_px = analysis.sell_target_max

        # 5. UserWatchItem 확인
        stmt = select(UserWatchItem).where(
            UserWatchItem.user_id == user_id,
            UserWatchItem.instrument_id == instrument.id
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # 업데이트 (수량, 목표가, updated_at 갱신)
            update_values = {
                "quantity": quantity,
                "updated_at": datetime.now(timezone.utc)
            }

            # 분석 결과가 있으면 목표가 업데이트
            if desired_buy_px is not None:
                update_values["desired_buy_px"] = desired_buy_px
            if target_sell_px is not None:
                update_values["target_sell_px"] = target_sell_px

            # broker_account_id가 제공되면 업데이트
            if broker_account_id is not None:
                update_values["broker_account_id"] = broker_account_id

            stmt = update(UserWatchItem).where(
                UserWatchItem.id == existing.id
            ).values(**update_values)
            await db.execute(stmt)
        else:
            # 신규 추가
            watch_item = UserWatchItem(
                user_id=user_id,
                instrument_id=instrument.id,
                broker_account_id=broker_account_id,
                quantity=quantity,
                desired_buy_px=desired_buy_px if desired_buy_px else current_price,
                target_sell_px=target_sell_px,
                notify_cooldown=timedelta(hours=1),  # 1시간 쿨다운
                is_active=True
            )
            db.add(watch_item)

    async def _get_or_create_exchange(
        self,
        db: AsyncSession,
        code: str
    ) -> Exchange:
        """거래소 조회 또는 생성"""
        stmt = select(Exchange).where(Exchange.code == code)
        result = await db.execute(stmt)
        exchange = result.scalar_one_or_none()

        if not exchange:
            # 거래소명 매핑
            exchange_names = {
                "KRX": "한국거래소",
                "NASDAQ": "나스닥",
                "NYSE": "뉴욕증권거래소",
                "AMEX": "아메리칸증권거래소",
                "UPBIT": "업비트"
            }

            exchange = Exchange(
                code=code,
                name=exchange_names.get(code, code),
                country="KR" if code in ["KRX", "UPBIT"] else "US",
                tz="Asia/Seoul" if code in ["KRX", "UPBIT"] else "America/New_York"
            )
            db.add(exchange)
            await db.flush()  # ID 생성을 위해 flush

        return exchange

    async def _get_or_create_instrument(
        self,
        db: AsyncSession,
        exchange_id: int,
        symbol: str,
        name: str,
        instrument_type: InstrumentType
    ) -> Instrument:
        """종목 조회 또는 생성"""
        stmt = select(Instrument).where(
            Instrument.exchange_id == exchange_id,
            Instrument.symbol == symbol
        )
        result = await db.execute(stmt)
        instrument = result.scalar_one_or_none()

        if not instrument:
            # 기본 통화 설정
            base_currency = "KRW" if instrument_type in [InstrumentType.equity_kr, InstrumentType.crypto] else "USD"

            instrument = Instrument(
                exchange_id=exchange_id,
                symbol=symbol,
                name=name,
                type=instrument_type,
                base_currency=base_currency,
                is_active=True
            )
            db.add(instrument)
            await db.flush()  # ID 생성을 위해 flush

        return instrument

    async def get_all_holdings(
        self,
        db: AsyncSession,
        user_id: int = 1,
        instrument_type: InstrumentType = None
    ) -> List[Dict[str, Any]]:
        """모든 보유 자산 조회

        Parameters
        ----------
        db : AsyncSession
            데이터베이스 세션
        user_id : int
            사용자 ID
        instrument_type : InstrumentType, optional
            특정 타입만 조회

        Returns
        -------
        List[Dict[str, Any]]
            보유 자산 리스트
        """
        from sqlalchemy.orm import aliased
        from sqlalchemy import func

        # 최신 분석 결과를 위한 서브쿼리
        latest_analysis_subq = (
            select(
                StockAnalysisResult.stock_info_id,
                func.max(StockAnalysisResult.created_at).label('max_created_at')
            )
            .group_by(StockAnalysisResult.stock_info_id)
            .subquery()
        )

        # 메인 쿼리
        stmt = (
            select(UserWatchItem, Instrument, Exchange, StockInfo, StockAnalysisResult)
            .join(Instrument, UserWatchItem.instrument_id == Instrument.id)
            .join(Exchange, Instrument.exchange_id == Exchange.id)
            .outerjoin(StockInfo, StockInfo.symbol == Instrument.symbol)
            .outerjoin(
                latest_analysis_subq,
                latest_analysis_subq.c.stock_info_id == StockInfo.id
            )
            .outerjoin(
                StockAnalysisResult,
                (StockAnalysisResult.stock_info_id == StockInfo.id) &
                (StockAnalysisResult.created_at == latest_analysis_subq.c.max_created_at)
            )
            .where(UserWatchItem.user_id == user_id)
            .where(UserWatchItem.is_active == True)
        )

        if instrument_type:
            stmt = stmt.where(Instrument.type == instrument_type)

        stmt = stmt.order_by(UserWatchItem.updated_at.desc())

        result = await db.execute(stmt)
        rows = result.all()

        holdings = []
        for watch_item, instrument, exchange, stock_info, analysis in rows:
            # user_watch_items의 값을 우선 사용, 없으면 분석 결과 사용
            desired_buy_px = watch_item.desired_buy_px
            target_sell_px = watch_item.target_sell_px

            # 분석 결과가 있고 watch_item에 값이 없으면 분석 결과 사용
            if analysis:
                if desired_buy_px is None and analysis.buy_hope_min is not None:
                    desired_buy_px = analysis.buy_hope_min
                if target_sell_px is None and analysis.sell_target_max is not None:
                    target_sell_px = analysis.sell_target_max

            # 현재가는 분석 결과의 current_price 사용 (분석 시점의 가격)
            current_price = None
            if analysis and analysis.current_price:
                current_price = float(analysis.current_price)
            elif stock_info and stock_info.current_price:
                # fallback: stock_info의 current_price (향후 제거 예정)
                current_price = float(stock_info.current_price)

            holdings.append({
                "id": watch_item.id,
                "symbol": instrument.symbol,
                "name": instrument.name,
                "instrument_type": instrument.type.value,
                "exchange": exchange.name,
                "exchange_code": exchange.code,
                "quantity": float(watch_item.quantity) if watch_item.quantity else 0,
                "current_price": current_price,
                "desired_buy_px": float(desired_buy_px) if desired_buy_px else None,
                "target_sell_px": float(target_sell_px) if target_sell_px else None,
                "stop_loss_px": float(watch_item.stop_loss_px) if watch_item.stop_loss_px else None,
                "note": watch_item.note,
                "has_analysis": analysis is not None,
                "analysis_date": analysis.created_at if analysis else None,
                "created_at": watch_item.created_at,
                "updated_at": watch_item.updated_at,
            })

        return holdings

    async def _get_or_update_stock_info(
        self,
        db: AsyncSession,
        symbol: str,
        name: str,
        instrument_type: str,
        exchange: str,
        current_price: float = None
    ) -> StockInfo:
        """StockInfo 조회 또는 생성

        Note: current_price는 더 이상 stock_info에 저장하지 않음 (stock_analysis_results에 저장)
        """
        from app.services.stock_info_service import create_stock_if_not_exists

        # stock_info 조회 또는 생성
        stock_info = await create_stock_if_not_exists(
            db=db,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type,
            exchange=exchange
        )

        return stock_info

    async def _get_latest_analysis(
        self,
        db: AsyncSession,
        stock_info_id: int = None
    ) -> StockAnalysisResult:
        """최신 분석 결과 조회"""
        if not stock_info_id:
            return None

        from sqlalchemy import func

        stmt = (
            select(StockAnalysisResult)
            .where(StockAnalysisResult.stock_info_id == stock_info_id)
            .order_by(StockAnalysisResult.created_at.desc())
            .limit(1)
        )

        result = await db.execute(stmt)
        return result.scalar_one_or_none()
