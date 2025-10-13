from typing import List

import pandas as pd

from app.services import upbit, yahoo, kis
from data.coins_info import upbit_pairs
from data.stocks_info import KRX_NAME_TO_CODE, get_exchange_by_symbol

from .analyzer import Analyzer, DataProcessor


class UpbitAnalyzer(Analyzer):
    """Upbit 암호화폐 분석기"""
    # 상수를 클래스 속성으로 정의합니다.
    MIN_TRADE_THRESHOLD = 1000  # 1000원 미만은 거래 불가로 간주
    
    def __init__(self, api_key=None):
        super().__init__(api_key)
        self._tradable_coins_map = None  # 캐시용 인스턴스 변수

    @staticmethod
    def _is_tradable(coin: dict) -> bool:
        """
        코인의 평가액이 최소 거래 금액 이상인지 확인하는 내부 유틸리티 메서드.
        (self를 사용하지 않으므로 @staticmethod로 선언)
        """
        try:
            balance = float(coin["balance"])
            locked = float(coin["locked"])
            avg_price = float(coin["avg_buy_price"])
            estimated_value = (balance + locked) * avg_price
            # 클래스 속성인 MIN_TRADE_THRESHOLD를 사용합니다.
            return estimated_value >= UpbitAnalyzer.MIN_TRADE_THRESHOLD
        except (ValueError, KeyError, TypeError):
            return False
    
    async def _get_tradable_coins_map(self, force_refresh: bool = False) -> dict:
        """
        거래 가능한 코인 맵을 가져오거나 캐시에서 반환
        
        Args:
            force_refresh: True일 경우 캐시를 무시하고 새로 가져옴
            
        Returns:
            심볼별 코인 정보 딕셔너리
        """
        if self._tradable_coins_map is None or force_refresh:
            try:
                my_coins = await upbit.fetch_my_coins()
                self._tradable_coins_map = {
                    f"KRW-{coin['currency']}": coin for coin in my_coins
                    if coin.get("currency") != "KRW"  # 원화 제외
                    and self._is_tradable(coin)  # 최소 평가액 이상
                    and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS  # KRW 마켓에서 거래 가능
                }
            except Exception as e:
                print(f"보유 자산 정보를 가져오는 데 실패했습니다: {e}")
                self._tradable_coins_map = {}
        
        return self._tradable_coins_map
    
    def _create_position_info(self, my_coin: dict) -> dict:
        """
        코인 정보를 position_info 형태로 변환
        
        Args:
            my_coin: 보유 코인 정보
            
        Returns:
            position_info 딕셔너리
        """
        if not my_coin:
            return None
            
        return {
            "quantity": my_coin.get("balance"),
            "avg_price": my_coin.get("avg_buy_price"),
            "total_value": (
                float(my_coin.get("balance", 0)) * float(my_coin.get("avg_buy_price", 0))
                if my_coin.get("balance") and my_coin.get("avg_buy_price") else None
            ),
            "locked_quantity": my_coin.get("locked"),
        }
    
    async def _collect_coin_data(self, stock_symbol: str):
        """
        코인 데이터 수집 (OHLCV, 현재가, 기본정보, 분봉)
        
        Args:
            stock_symbol: 코인 심볼 (예: KRW-BTC)
            
        Returns:
            (df_merged, fundamental_info, minute_candles) 튜플
        """
        # 기본 데이터 수집
        df_historical = await upbit.fetch_ohlcv(stock_symbol, days=200)
        df_current = await upbit.fetch_price(stock_symbol)
        fundamental_info = await upbit.fetch_fundamental_info(stock_symbol)
        
        # 분봉 데이터 수집
        minute_candles = {}
        try:
            # 60분 캔들 (최근 12개)
            df_60min = await upbit.fetch_hourly_candles(stock_symbol, count=12)
            minute_candles["60min"] = df_60min
            
            # 5분 캔들 (최근 12개)
            df_5min = await upbit.fetch_5min_candles(stock_symbol, count=12)
            minute_candles["5min"] = df_5min
            
            # 1분 캔들 (최근 10개)
            df_1min = await upbit.fetch_1min_candles(stock_symbol, count=10)
            minute_candles["1min"] = df_1min
        except Exception as e:
            print(f"분봉 데이터 수집 실패: {e}")
            minute_candles = {}

        # 데이터 병합
        df_merged = DataProcessor.merge_historical_and_current(
            df_historical, df_current
        )
        
        return df_merged, fundamental_info, minute_candles

    async def analyze_coins(self, coin_names: List[str]) -> None:
        """여러 코인을 순차적으로 분석"""
        await upbit_pairs.prime_upbit_constants()
        
        # 보유 코인 정보를 한 번만 가져와서 캐시
        tradable_coins_map = await self._get_tradable_coins_map()
        
        for coin_name in coin_names:
            stock_symbol = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
            if not stock_symbol:
                print(f"코인명을 찾을 수 없음: {coin_name}")
                continue
            
            # 캐시된 정보에서 코인 조회
            my_coin = tradable_coins_map.get(stock_symbol)
            position_info = self._create_position_info(my_coin)

            print(f"\n=== {coin_name} ({stock_symbol}) 분석 시작 ===")

            # 데이터 수집
            df_merged, fundamental_info, minute_candles = await self._collect_coin_data(stock_symbol)

            # 분석 및 저장
            result, model_name = await self.analyze_and_save(
                df=df_merged,
                symbol=stock_symbol,
                name=coin_name,
                instrument_type="crypto",
                currency="₩",
                unit_shares="개",
                fundamental_info=fundamental_info,
                position_info=position_info,
                minute_candles=minute_candles,
            )

            print(f"분석 완료: {coin_name}")
            print(f"결과: {result[:100]}...")

    async def analyze_coins_json(self, coin_names: List[str]) -> None:
        """여러 코인을 순차적으로 JSON 형식으로 분석"""
        await upbit_pairs.prime_upbit_constants()
        
        # 보유 코인 정보를 한 번만 가져와서 캐시
        tradable_coins_map = await self._get_tradable_coins_map()
            
        for coin_name in coin_names:
            stock_symbol = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
            if not stock_symbol:
                print(f"코인명을 찾을 수 없음: {coin_name}")
                continue
                
            # 캐시된 정보에서 코인 조회
            my_coin = tradable_coins_map.get(stock_symbol)
            position_info = self._create_position_info(my_coin)

            print(f"\n=== {coin_name} ({stock_symbol}) JSON 분석 시작 ===")

            # 데이터 수집
            df_merged, fundamental_info, minute_candles = await self._collect_coin_data(stock_symbol)

            # JSON 형식으로 분석 및 저장
            result, model_name = await self.analyze_and_save_json(
                df=df_merged,
                symbol=stock_symbol,
                name=coin_name,
                instrument_type="crypto",
                currency="₩",
                unit_shares="개",
                fundamental_info=fundamental_info,
                position_info=position_info,
                minute_candles=minute_candles,
            )

            print(f"JSON 분석 완료: {coin_name}")
            if hasattr(result, 'decision'):
                print(f"결정: {result.decision}, 신뢰도: {result.confidence}%")
                print(f"매수 범위: {result.price_analysis.appropriate_buy_range.min:,.0f}원 ~ {result.price_analysis.appropriate_buy_range.max:,.0f}원")
            else:
                print(f"결과: {result[:100]}...")

    async def analyze_coin_json(self, coin_name: str) -> None:
        """단일 코인을 JSON 형식으로 분석"""
        await upbit_pairs.prime_upbit_constants()
        
        stock_symbol = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
        if not stock_symbol:
            print(f"코인명을 찾을 수 없음: {coin_name}")
            return
        
        # 보유 코인 정보 가져오기 (캐시 사용)
        tradable_coins_map = await self._get_tradable_coins_map()
        my_coin = tradable_coins_map.get(stock_symbol)
        position_info = self._create_position_info(my_coin)

        print(f"\n=== {coin_name} ({stock_symbol}) JSON 분석 시작 ===")

        # 데이터 수집
        df_merged, fundamental_info, minute_candles = await self._collect_coin_data(stock_symbol)

        # JSON 형식으로 분석 및 저장
        result, model_name = await self.analyze_and_save_json(
            df=df_merged,
            symbol=stock_symbol,
            name=coin_name,
            instrument_type="crypto",
            currency="₩",
            unit_shares="개",
            fundamental_info=fundamental_info,
            position_info=position_info,
            minute_candles=minute_candles,
        )

        print(f"JSON 분석 완료: {coin_name}")
        if hasattr(result, 'decision'):
            print(f"결정: {result.decision}, 신뢰도: {result.confidence}%")
            print(f"매수 범위: {result.price_analysis.appropriate_buy_range.min:,.0f}원 ~ {result.price_analysis.appropriate_buy_range.max:,.0f}원")
        else:
            print(f"결과: {result[:100]}...")


class YahooAnalyzer(Analyzer):
    """Yahoo Finance 주식 분석기"""
    
    async def _collect_stock_data(self, stock_symbol: str):
        """
        주식 데이터 수집 (OHLCV, 현재가, 기본정보)
        
        Args:
            stock_symbol: 주식 심볼 (예: AAPL)
            
        Returns:
            (df_merged, fundamental_info) 튜플
        """
        # 기본 데이터 수집
        df_historical = await yahoo.fetch_ohlcv(stock_symbol, 200)
        df_current = await yahoo.fetch_price(stock_symbol)
        fundamental_info = await yahoo.fetch_fundamental_info(stock_symbol)

        # 데이터 병합
        df_merged = DataProcessor.merge_historical_and_current(
            df_historical, df_current
        )
        
        return df_merged, fundamental_info
    
    def _print_analysis_result(self, result, stock_symbol: str, use_json: bool = False):
        """
        분석 결과 출력
        
        Args:
            result: 분석 결과
            stock_symbol: 주식 심볼
            use_json: JSON 형식 여부
        """
        if use_json:
            print(f"JSON 분석 완료: {stock_symbol}")
            if hasattr(result, 'decision'):
                print(f"결정: {result.decision}, 신뢰도: {result.confidence}%")
                print(f"매수 범위: ${result.price_analysis.appropriate_buy_range.min:.2f} ~ ${result.price_analysis.appropriate_buy_range.max:.2f}")
            else:
                print(f"결과: {result[:100]}...")
        else:
            print(f"분석 완료: {stock_symbol}")
            print(f"결과: {result[:100]}...")

    async def analyze_stocks(self, stock_symbols: List[str]) -> None:
        """여러 주식을 순차적으로 분석"""

        for stock_symbol in stock_symbols:
            print(f"\n=== {stock_symbol} 분석 시작 ===")

            # 데이터 수집
            df_merged, fundamental_info = await self._collect_stock_data(stock_symbol)

            # 분석 및 저장
            result, model_name = await self.analyze_and_save(
                df=df_merged,
                symbol=stock_symbol,
                name=stock_symbol,
                instrument_type="equity_us",
                currency="$",
                unit_shares="주",
                fundamental_info=fundamental_info,
                minute_candles=None,  # Yahoo는 분봉 데이터를 지원하지 않음
            )

            self._print_analysis_result(result, stock_symbol, use_json=False)

    async def analyze_stocks_json(self, stock_symbols: List[str]) -> None:
        """여러 주식을 순차적으로 JSON 형식으로 분석"""

        for stock_symbol in stock_symbols:
            print(f"\n=== {stock_symbol} JSON 분석 시작 ===")

            # 데이터 수집
            df_merged, fundamental_info = await self._collect_stock_data(stock_symbol)

            # JSON 형식으로 분석 및 저장
            result, model_name = await self.analyze_and_save_json(
                df=df_merged,
                symbol=stock_symbol,
                name=stock_symbol,
                instrument_type="equity_us",
                currency="$",
                unit_shares="주",
                fundamental_info=fundamental_info,
                minute_candles=None,  # Yahoo는 분봉 데이터를 지원하지 않음
            )

            self._print_analysis_result(result, stock_symbol, use_json=True)

    async def analyze_stock_json(self, stock_symbol: str) -> None:
        """단일 주식을 JSON 형식으로 분석"""
        print(f"\n=== {stock_symbol} JSON 분석 시작 ===")

        # 데이터 수집
        df_merged, fundamental_info = await self._collect_stock_data(stock_symbol)

        # JSON 형식으로 분석 및 저장
        result, model_name = await self.analyze_and_save_json(
            df=df_merged,
            symbol=stock_symbol,
            name=stock_symbol,
            instrument_type="equity_us",
            currency="$",
            unit_shares="주",
            fundamental_info=fundamental_info,
            minute_candles=None,  # Yahoo는 분봉 데이터를 지원하지 않음
        )

        self._print_analysis_result(result, stock_symbol, use_json=True)


class KISAnalyzer(Analyzer):
    """KIS 국내주식 및 해외주식 분석기"""

    async def _collect_stock_data(self, stock_name: str, stock_code: str):
        """
        국내 주식 데이터 수집 (OHLCV, 현재가, 기본정보, 분봉)

        Args:
            stock_name: 종목명
            stock_code: 종목코드

        Returns:
            (df_merged, fundamental_info, minute_candles) 튜플
        """
        # 기본 데이터 수집
        df_historical = await kis.kis.inquire_daily_itemchartprice(stock_code)
        df_current = await kis.kis.inquire_price(stock_code)
        fundamental_info = await kis.kis.fetch_fundamental_info(stock_code)

        # 분봉 데이터 수집
        minute_candles = {}
        try:
            minute_candles = await kis.kis.fetch_minute_candles(stock_code)
        except Exception as e:
            print(f"분봉 데이터 수집 실패: {e}")
            minute_candles = {}

        # 데이터 병합
        df_merged = DataProcessor.merge_historical_and_current(
            df_historical, df_current
        )

        return df_merged, fundamental_info, minute_candles

    async def _collect_overseas_stock_data(self, symbol: str, exchange_code: str):
        """
        해외 주식 데이터 수집 (OHLCV, 현재가, 기본정보, 분봉)

        Args:
            symbol: 주식 심볼 (예: "AAPL")
            exchange_code: 거래소 코드 (예: "NASD", "NYSE", "AMEX")

        Returns:
            (df_merged, fundamental_info, minute_candles) 튜플
        """
        # 기본 데이터 수집
        # 일봉 데이터: KIS API에서 일봉을 지원하지 않는 경우를 대비하여 빈 DataFrame 사용
        try:
            df_historical = await kis.kis.inquire_overseas_daily_price(symbol, exchange_code)
        except Exception as e:
            print(f"일봉 데이터 수집 실패 (Yahoo Finance 방식으로 대체 필요): {e}")
            # 빈 DataFrame 생성 (Yahoo Finance와 동일한 컬럼)
            df_historical = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        df_current = await kis.kis.inquire_overseas_price(symbol, exchange_code)
        fundamental_info = await kis.kis.fetch_overseas_fundamental_info(symbol, exchange_code)

        # 분봉 데이터 수집
        minute_candles = {}
        try:
            minute_candles = await kis.kis.fetch_overseas_minute_candles(symbol, exchange_code)
        except Exception as e:
            print(f"분봉 데이터 수집 실패: {e}")
            minute_candles = {}

        # 데이터 병합 (일봉이 없어도 현재가만으로 분석 가능)
        if df_historical.empty:
            # 일봉 데이터가 없으면 현재가만 사용
            df_merged = df_current
        else:
            df_merged = DataProcessor.merge_historical_and_current(
                df_historical, df_current
            )

        return df_merged, fundamental_info, minute_candles
    
    def _print_analysis_result(self, result, stock_name: str, use_json: bool = False):
        """
        분석 결과 출력
        
        Args:
            result: 분석 결과
            stock_name: 종목명
            use_json: JSON 형식 여부
        """
        if use_json:
            print(f"JSON 분석 완료: {stock_name}")
            if hasattr(result, 'decision'):
                print(f"결정: {result.decision}, 신뢰도: {result.confidence}%")
                print(f"매수 범위: {result.price_analysis.appropriate_buy_range.min:,.0f}원 ~ {result.price_analysis.appropriate_buy_range.max:,.0f}원")
            else:
                print(f"결과: {result[:100]}...")
        else:
            print(f"분석 완료: {stock_name}")
            print(f"결과: {result[:100]}...")

    async def analyze_stock(self, stock_name: str) -> None:
        """단일 국내주식 분석"""
        print(f"\n=== {stock_name} 분석 시작 ===")

        # 종목 코드 조회
        stock_code = KRX_NAME_TO_CODE.get(stock_name)
        if not stock_code:
            print(f"종목명을 찾을 수 없음: {stock_name}")
            return

        # 데이터 수집
        df_merged, fundamental_info, minute_candles = await self._collect_stock_data(stock_name, stock_code)

        # 분석 및 저장
        result, model_name = await self.analyze_and_save(
            df=df_merged,
            symbol=stock_code,
            name=stock_name,
            instrument_type="equity_kr",
            currency="₩",
            unit_shares="주",
            fundamental_info=fundamental_info,
            minute_candles=minute_candles,
        )

        self._print_analysis_result(result, stock_name, use_json=False)

    async def analyze_stocks(self, stock_names: List[str]) -> None:
        """여러 국내주식을 순차적으로 분석"""
        for stock_name in stock_names:
            await self.analyze_stock(stock_name)

    async def analyze_stocks_json(self, stock_names: List[str]) -> None:
        """여러 국내주식을 순차적으로 JSON 형식으로 분석"""
        for stock_name in stock_names:
            await self.analyze_stock_json(stock_name)

    async def analyze_stock_json(self, stock_name: str) -> None:
        """단일 국내주식을 JSON 형식으로 분석"""
        print(f"\n=== {stock_name} JSON 분석 시작 ===")

        # 종목 코드 조회
        stock_code = KRX_NAME_TO_CODE.get(stock_name)
        if not stock_code:
            print(f"종목명을 찾을 수 없음: {stock_name}")
            return

        # 데이터 수집
        df_merged, fundamental_info, minute_candles = await self._collect_stock_data(stock_name, stock_code)

        # JSON 형식으로 분석 및 저장
        result, model_name = await self.analyze_and_save_json(
            df=df_merged,
            symbol=stock_code,
            name=stock_name,
            instrument_type="equity_kr",
            currency="₩",
            unit_shares="주",
            fundamental_info=fundamental_info,
            minute_candles=minute_candles,
        )

        self._print_analysis_result(result, stock_name, use_json=True)

    async def analyze_overseas_stocks(self, stock_symbols: List[str]) -> None:
        """여러 해외주식을 순차적으로 분석"""
        for symbol in stock_symbols:
            await self.analyze_overseas_stock(symbol)

    async def analyze_overseas_stock(self, symbol: str) -> None:
        """단일 해외주식 분석"""
        print(f"\n=== {symbol} 해외주식 분석 시작 ===")

        # 거래소 코드 자동 조회
        exchange_code = get_exchange_by_symbol(symbol)
        if not exchange_code:
            print(f"심볼을 찾을 수 없음: {symbol}")
            return

        print(f"거래소: {exchange_code}")

        # 데이터 수집
        df_merged, fundamental_info, minute_candles = await self._collect_overseas_stock_data(
            symbol, exchange_code
        )

        # 분석 및 저장
        result, model_name = await self.analyze_and_save(
            df=df_merged,
            symbol=symbol,
            name=symbol,
            instrument_type="equity_us",
            currency="$",
            unit_shares="주",
            fundamental_info=fundamental_info,
            minute_candles=minute_candles,
        )

        self._print_analysis_result(result, symbol, use_json=False)

    async def analyze_overseas_stocks_json(self, stock_symbols: List[str]) -> None:
        """여러 해외주식을 순차적으로 JSON 형식으로 분석"""
        for symbol in stock_symbols:
            await self.analyze_overseas_stock_json(symbol)

    async def analyze_overseas_stock_json(self, symbol: str) -> None:
        """단일 해외주식을 JSON 형식으로 분석"""
        print(f"\n=== {symbol} 해외주식 JSON 분석 시작 ===")

        # 거래소 코드 자동 조회
        exchange_code = get_exchange_by_symbol(symbol)
        if not exchange_code:
            print(f"심볼을 찾을 수 없음: {symbol}")
            return

        print(f"거래소: {exchange_code}")

        # 데이터 수집
        df_merged, fundamental_info, minute_candles = await self._collect_overseas_stock_data(
            symbol, exchange_code
        )

        # JSON 형식으로 분석 및 저장
        result, model_name = await self.analyze_and_save_json(
            df=df_merged,
            symbol=symbol,
            name=symbol,
            instrument_type="equity_us",
            currency="$",
            unit_shares="주",
            fundamental_info=fundamental_info,
            minute_candles=minute_candles,
        )

        self._print_analysis_result(result, symbol, use_json=True)
