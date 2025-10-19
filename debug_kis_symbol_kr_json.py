#!/usr/bin/env python3
"""
KIS 한국 주식 JSON 분석 실행 예시
"""

import asyncio
from app.analysis.service_analyzers import KISAnalyzer
from app.services.kis import kis


async def main():
    # JSON 분석기 초기화
    analyzer = KISAnalyzer()

    try:
        print("=" * 70)
        print("=== 보유 국내주식 조회 및 분석 ===")
        print("=" * 70)

        # 국내주식 조회 (is_overseas=False)
        kr_stocks = await kis.fetch_my_stocks(is_mock=False, is_overseas=False)

        if not kr_stocks:
            print("보유 중인 국내 주식이 없습니다.\n")
            return

        print(f"총 {len(kr_stocks)}개 종목 보유 중\n")

        # 보유 주식 정보 출력
        for stock in kr_stocks:
            stock_code = stock.get('pdno')  # 종목코드
            stock_name = stock.get('prdt_name')  # 종목명
            quantity = stock.get('hldg_qty')  # 보유수량
            avg_price = stock.get('pchs_avg_pric')  # 매입평균가격
            current_price = stock.get('prpr')  # 현재가
            eval_amount = stock.get('evlu_amt')  # 평가금액
            profit_loss = stock.get('evlu_pfls_amt')  # 평가손익금액
            profit_rate = stock.get('evlu_pfls_rt')  # 평가손익율

            # 문자열을 숫자로 변환 (KIS API는 문자열로 반환)
            try:
                quantity_num = int(quantity) if quantity else 0
                avg_price_num = float(avg_price) if avg_price else 0
                current_price_num = float(current_price) if current_price else 0
                eval_amount_num = float(eval_amount) if eval_amount else 0
                profit_loss_num = float(profit_loss) if profit_loss else 0

                print(f"  - {stock_name} ({stock_code})")
                print(f"      보유: {quantity_num}주 | 평균매입: {avg_price_num:,.0f}원 | 현재가: {current_price_num:,.0f}원")
                print(f"      평가금액: {eval_amount_num:,.0f}원 | 손익: {profit_loss_num:,.0f}원 ({profit_rate}%)")
            except (ValueError, TypeError) as e:
                # 변환 실패 시 원본 값 그대로 출력
                print(f"  - {stock_name} ({stock_code})")
                print(f"      보유: {quantity}주 | 평균매입: {avg_price}원 | 현재가: {current_price}원")
                print(f"      평가금액: {eval_amount}원 | 손익: {profit_loss}원 ({profit_rate}%)")
                print(f"      (포맷팅 실패: {e})")

        print("\n" + "=" * 70)

        # 각 종목에 대해 JSON 분석 실행
        for stock in kr_stocks:
            stock_name = stock.get('prdt_name')  # 종목명

            try:
                await analyzer.analyze_stock_json(stock_name)
            except Exception as e:
                print(f"  ❌ {stock_name} 분석 실패: {e}")
                import traceback
                traceback.print_exc()
                continue

    except Exception as e:
        print(f"에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
