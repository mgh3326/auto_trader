#!/usr/bin/env python3
"""
JSON 형식 주식 분석 사용 예시
"""

import asyncio
import pandas as pd
from app.analysis import Analyzer, StockAnalysisResponse


async def main():
    """JSON 분석 예시"""
    
    # 분석기 생성
    analyzer = Analyzer()
    
    # 샘플 데이터 생성 (실제로는 실제 주식 데이터를 사용)
    dates = pd.date_range('2024-01-01', periods=100, freq='D')
    sample_data = pd.DataFrame({
        'date': dates,
        'open': [100 + i * 0.1 for i in range(100)],
        'high': [102 + i * 0.1 for i in range(100)],
        'low': [98 + i * 0.1 for i in range(100)],
        'close': [101 + i * 0.1 for i in range(100)],
        'volume': [1000000 + i * 1000 for i in range(100)],
        'value': [100000000 + i * 100000 for i in range(100)]
    })
    
    try:
        # JSON 형식으로 분석 실행
        result, model_name = await analyzer.analyze_and_save_json(
            df=sample_data,
            symbol="005930",  # 삼성전자
            name="삼성전자",
            instrument_type="equity_kr",
            currency="₩",
            unit_shares="주",
            fundamental_info={
                "시가총액": "500조원",
                "PER": 15.2,
                "PBR": 1.8
            },
            position_info={
                "quantity": 100,
                "avg_price": 75000,
                "total_value": 7500000
            }
        )
        
        # 결과 출력
        print(f"모델: {model_name}")
        print(f"결정: {result.decision}")
        print(f"신뢰도: {result.confidence}%")
        print("\n근거:")
        for i, reason in enumerate(result.reasons, 1):
            print(f"{i}. {reason}")
        
        print("\n가격 분석:")
        print(f"적절한 매수 범위: {result.price_analysis.appropriate_buy_range.min:,}원 ~ {result.price_analysis.appropriate_buy_range.max:,}원")
        print(f"적절한 매도 범위: {result.price_analysis.appropriate_sell_range.min:,}원 ~ {result.price_analysis.appropriate_sell_range.max:,}원")
        print(f"매수 희망 범위: {result.price_analysis.buy_hope_range.min:,}원 ~ {result.price_analysis.buy_hope_range.max:,}원")
        print(f"매도 목표 범위: {result.price_analysis.sell_target_range.min:,}원 ~ {result.price_analysis.sell_target_range.max:,}원")
        
        print("\n상세 분석:")
        print(result.detailed_text)
        
    except Exception as e:
        print(f"분석 중 오류 발생: {e}")
    
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
