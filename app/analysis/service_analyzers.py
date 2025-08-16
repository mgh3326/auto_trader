from typing import List

import pandas as pd

from app.services import upbit, yahoo, kis
from data.coins_info import upbit_pairs
from data.stocks_info import KRX_NAME_TO_CODE

from .analyzer import Analyzer, DataProcessor


class UpbitAnalyzer(Analyzer):
    """Upbit 암호화폐 분석기"""

    async def analyze_coins(self, coin_names: List[str]) -> None:
        """여러 코인을 순차적으로 분석"""
        await upbit_pairs.prime_upbit_constants()

        for coin_name in coin_names:
            stock_symbol = upbit_pairs.NAME_TO_PAIR_KR.get(coin_name)
            if not stock_symbol:
                print(f"코인명을 찾을 수 없음: {coin_name}")
                continue

            print(f"\n=== {coin_name} ({stock_symbol}) 분석 시작 ===")

            # 데이터 수집
            df_historical = await upbit.fetch_ohlcv(stock_symbol, days=200)
            df_current = await upbit.fetch_price(stock_symbol)
            fundamental_info = await upbit.fetch_fundamental_info(stock_symbol)

            # 데이터 병합
            df_merged = DataProcessor.merge_historical_and_current(
                df_historical, df_current
            )

            # 분석 및 저장
            result, model_name = await self.analyzeAnd_save(
                df=df_merged,
                symbol=stock_symbol,
                name=coin_name,
                instrument_type="crypto",
                currency="₩",
                unit_shares="개",
                fundamental_info=fundamental_info,
            )

            print(f"분석 완료: {coin_name}")
            print(f"결과: {result[:100]}...")


class YahooAnalyzer(Analyzer):
    """Yahoo Finance 주식 분석기"""

    async def analyze_stocks(self, stock_symbols: List[str]) -> None:
        """여러 주식을 순차적으로 분석"""

        for stock_symbol in stock_symbols:
            print(f"\n=== {stock_symbol} 분석 시작 ===")

            # 데이터 수집
            df_historical = await yahoo.fetch_ohlcv(stock_symbol, 200)
            df_current = await yahoo.fetch_price(stock_symbol)
            fundamental_info = await yahoo.fetch_fundamental_info(stock_symbol)

            # 데이터 병합
            df_merged = DataProcessor.merge_historical_and_current(
                df_historical, df_current
            )

            # 분석 및 저장
            result, model_name = await self.analyze_and_save(
                df=df_merged,
                symbol=stock_symbol,
                name=stock_symbol,
                instrument_type="equity_us",
                currency="$",
                unit_shares="주",
                fundamental_info=fundamental_info,
            )

            print(f"분석 완료: {stock_symbol}")
            print(f"결과: {result[:100]}...")


class KISAnalyzer(Analyzer):
    """KIS 국내주식 분석기"""

    async def analyze_stock(self, stock_name: str) -> None:
        """단일 국내주식 분석"""
        print(f"\n=== {stock_name} 분석 시작 ===")

        # 종목 코드 조회
        stock_code = KRX_NAME_TO_CODE.get(stock_name)
        if not stock_code:
            print(f"종목명을 찾을 수 없음: {stock_name}")
            return

        # 데이터 수집
        df_historical = await kis.kis.inquire_daily_itemchartprice(stock_code)
        df_current = await kis.kis.inquire_price(stock_code)
        fundamental_info = await kis.kis.fetch_fundamental_info(stock_code)
        # 데이터 병합
        df_merged = DataProcessor.merge_historical_and_current(
            df_historical, df_current
        )

        # 분석 및 저장
        result, model_name = await self.analyze_and_save(
            df=df_merged,
            symbol=stock_code,
            name=stock_name,
            instrument_type="equity_kr",
            currency="₩",
            unit_shares="주",
            fundamental_info=fundamental_info,
        )

        print(f"분석 완료: {stock_name}")
        print(f"결과: {result[:100]}...")

    async def analyze_stocks(self, stock_names: List[str]) -> None:
        """여러 국내주식을 순차적으로 분석"""
        for stock_name in stock_names:
            await self.analyze_stock(stock_name)
