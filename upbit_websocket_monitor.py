#!/usr/bin/env python3
"""
업비트 WebSocket MyOrder 모니터링 실행 스크립트
매수/매도가 발생할 때마다 analyze_coin_json으로 단일 코인 분석을 수행합니다.
"""

import asyncio
import logging
from app.services.upbit_websocket import UpbitOrderAnalysisService
from app.analysis.service_analyzers import UpbitAnalyzer
from data.coins_info import upbit_pairs

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    """메인 실행 함수"""
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()
    
    # 분석기 초기화
    analyzer = UpbitAnalyzer()
    
    async def analyze_coin_callback(coin_name: str):
        """코인 분석 콜백 함수"""
        try:
            result, model = await analyzer.analyze_coin_json(coin_name)
            if result is None:
                logger.warning(f"코인 분석 실패: {coin_name}")
        except Exception as e:
            logger.error(f"코인 분석 오류 ({coin_name}): {e}")
    
    # WebSocket 모니터링 서비스 초기화 (SSL 검증 비활성화 - macOS 호환성)
    service = UpbitOrderAnalysisService(
        analyzer_callback=analyze_coin_callback,
        verify_ssl=False  # macOS에서 SSL 인증서 문제 해결
    )
    
    try:
        logger.info("업비트 주문/체결 모니터링을 시작합니다...")
        logger.info("매수/매도가 발생할 때마다 자동으로 해당 코인을 분석합니다.")
        logger.info("중지하려면 Ctrl+C를 누르세요.")
        
        # 모니터링 시작 (모든 코인 페어 구독)
        await service.start_monitoring()
        
        # 무한 대기 (KeyboardInterrupt로 종료)
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("사용자에 의해 중지되었습니다.")
    except Exception as e:
        logger.error(f"오류 발생: {e}")
    finally:
        # 정리 작업
        await service.stop_monitoring()
        await analyzer.close()
        logger.info("모니터링이 완전히 종료되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
