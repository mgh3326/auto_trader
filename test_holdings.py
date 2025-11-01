"""보유 자산 관리 기능 테스트 스크립트"""
import asyncio
from app.core.db import AsyncSessionLocal
from app.services.kis import KISClient
from app.services.holdings_service import HoldingsService
from app.core.config import settings


async def test_holdings():
    """보유 자산 조회 및 저장 테스트"""
    print("=" * 80)
    print("보유 자산 관리 테스트 시작")
    print("=" * 80)

    # DB 세션 생성
    async with AsyncSessionLocal() as db:
        # KIS 클라이언트 생성 (설정은 settings에서 자동으로 가져옴)
        kis_client = KISClient()

        # HoldingsService 생성
        holdings_service = HoldingsService(kis_client=kis_client)

        print("\n[1단계] 보유 자산 갱신 중...")
        print("-" * 80)

        # 실전 투자 데이터 가져오기 (is_mock=False)
        results = await holdings_service.fetch_and_update_all_holdings(
            db=db,
            user_id=1,
            is_mock=False  # 실전투자로 설정
        )

        print(f"\n✓ 갱신 완료 시각: {results['updated_at']}")
        print(f"✓ 국내주식: {results['kr_stocks']['count']}개")
        for item in results['kr_stocks']['items'][:5]:  # 최대 5개만 표시
            print(f"  - {item['symbol']}: {item['name']} ({item['quantity']}주)")

        print(f"\n✓ 미국주식: {results['us_stocks']['count']}개")
        for item in results['us_stocks']['items'][:5]:  # 최대 5개만 표시
            print(f"  - {item['symbol']}: {item['name']} ({item['quantity']}주)")

        print(f"\n✓ 암호화폐: {results['crypto']['count']}개")
        for item in results['crypto']['items'][:5]:  # 최대 5개만 표시
            print(f"  - {item['symbol']}: {item['name']} ({item['quantity']})")

        if results['errors']:
            print(f"\n⚠ 에러 {len(results['errors'])}건:")
            for error in results['errors'][:5]:  # 최대 5개만 표시
                print(f"  - {error}")

        print("\n[2단계] 저장된 보유 자산 조회 중...")
        print("-" * 80)

        # 저장된 데이터 조회
        holdings = await holdings_service.get_all_holdings(db=db, user_id=1)

        print(f"\n✓ 총 {len(holdings)}개 보유 자산")

        # 타입별 분류
        kr_stocks = [h for h in holdings if h["instrument_type"] == "equity_kr"]
        us_stocks = [h for h in holdings if h["instrument_type"] == "equity_us"]
        crypto = [h for h in holdings if h["instrument_type"] == "crypto"]

        print(f"  - 국내주식: {len(kr_stocks)}개")
        print(f"  - 미국주식: {len(us_stocks)}개")
        print(f"  - 암호화폐: {len(crypto)}개")

        print("\n[3단계] 최근 업데이트된 보유 자산 (상위 10개)")
        print("-" * 80)
        for i, holding in enumerate(holdings[:10], 1):
            print(f"{i}. [{holding['instrument_type']}] {holding['symbol']} - {holding['name']}")
            print(f"   보유수량: {holding['quantity']}, 거래소: {holding['exchange']}")
            print(f"   업데이트: {holding['updated_at']}")
            print()

    print("=" * 80)
    print("테스트 완료!")
    print("=" * 80)
    print("\n다음 단계:")
    print("1. 개발 서버 시작: make dev")
    print("2. 브라우저에서 접속: http://127.0.0.1:8000/holdings/")
    print("3. '갱신' 버튼 클릭하여 최신 데이터 가져오기")
    print("4. 필터로 상품 타입별 조회")


if __name__ == "__main__":
    asyncio.run(test_holdings())
