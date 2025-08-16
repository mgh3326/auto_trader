import asyncio
from app.analysis.service_analyzers import UpbitAnalyzer
from app.services import upbit
from data.coins_info import upbit_pairs


async def main():
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()
    
    # 분석기 초기화
    analyzer = UpbitAnalyzer()
    
    try:
        # 보유 코인 정보 가져오기
        my_coins = await upbit.fetch_my_coins()
        print(f"총 {len(my_coins)}개 코인 보유 중")
        
        # 거래 가능한 코인만 필터링 (원화 제외, 최소 평가액 이상, KRW 마켓 거래 가능)
        tradable_coins_list = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"  # 원화 제외
               and analyzer._is_tradable(coin)  # 최소 평가액 이상
               and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS  # KRW 마켓에서 거래 가능
        ]
        
        print(f"분석 가능한 코인: {len(tradable_coins_list)}개")
        for coin in tradable_coins_list:
            balance = float(coin.get("balance", 0))
            avg_price = float(coin.get("avg_buy_price", 0))
            total_value = balance * avg_price
            print(f"- {coin.get('currency')}: {balance:.8f}개, 평균 {avg_price:,.0f}₩, 총 {total_value:,.0f}₩")
        
        # 보유 코인의 한국 이름으로 coin_names 생성
        coin_names = []
        for coin in tradable_coins_list:
            currency = coin.get("currency")
            # COIN_TO_NAME_KR에서 한국 이름 찾기
            korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency)
            if korean_name:
                coin_names.append(korean_name)
            else:
                # 한국 이름이 없으면 영어 이름 사용
                english_name = upbit_pairs.COIN_TO_NAME_EN.get(currency, currency)
                coin_names.append(english_name)
        
        if not coin_names:
            print("분석 가능한 코인이 없습니다.")
            return
        
        print(f"\n분석할 코인 목록: {coin_names}")
        
        # 코인 분석 실행
        await analyzer.analyze_coins(coin_names)
        
    except Exception as e:
        print(f"에러 발생: {e}")
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
