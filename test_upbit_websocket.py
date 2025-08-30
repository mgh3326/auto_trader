#!/usr/bin/env python3
"""
업비트 WebSocket MyOrder 테스트 스크립트
실제 주문/체결 없이 WebSocket 연결과 메시지 구조를 테스트합니다.
"""

import asyncio
import logging
from app.services.upbit_websocket import UpbitMyOrderWebSocket, UpbitOrderAnalysisService
from data.coins_info import upbit_pairs

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def test_websocket_connection():
    """WebSocket 연결 테스트"""
    logger.info("=== WebSocket 연결 테스트 시작 ===")
    
    async def mock_order_callback(order_data: dict):
        """모의 주문 데이터 콜백"""
        logger.info(f"주문 데이터 수신: {order_data}")
    
    client = UpbitMyOrderWebSocket(
        on_order_callback=mock_order_callback,
        verify_ssl=False  # macOS에서 SSL 인증서 문제 해결
    )
    
    try:
        # 특정 코인만 테스트 (예: 비트코인, 이더리움)
        test_coins = ["KRW-BTC", "KRW-ETH"]
        
        logger.info(f"테스트 코인: {test_coins}")
        logger.info("WebSocket 연결을 시작합니다...")
        
        # 비동기로 연결 및 30초간 대기
        connection_task = asyncio.create_task(
            client.connect_and_subscribe(test_coins)
        )
        
        # 30초 후 타임아웃
        try:
            await asyncio.wait_for(connection_task, timeout=30.0)
        except asyncio.TimeoutError:
            logger.info("30초 테스트 완료")
        
    except Exception as e:
        logger.error(f"테스트 오류: {e}")
    finally:
        await client.disconnect()
        logger.info("=== WebSocket 연결 테스트 완료 ===")


async def test_analysis_service():
    """분석 서비스 테스트"""
    logger.info("=== 분석 서비스 테스트 시작 ===")
    
    async def mock_analyzer_callback(coin_name: str):
        """모의 분석 콜백"""
        logger.info(f"[모의 분석] {coin_name} 분석을 수행합니다...")
        await asyncio.sleep(1)  # 분석 시뮬레이션
        logger.info(f"[모의 분석] {coin_name} 분석 완료")
    
    service = UpbitOrderAnalysisService(
        analyzer_callback=mock_analyzer_callback,
        verify_ssl=False  # macOS에서 SSL 인증서 문제 해결
    )
    
    try:
        # 특정 코인만 모니터링
        test_coins = ["KRW-BTC", "KRW-ETH", "KRW-ADA"]
        
        logger.info(f"모니터링 코인: {test_coins}")
        
        # 비동기로 모니터링 시작
        monitor_task = asyncio.create_task(
            service.start_monitoring(test_coins)
        )
        
        # 60초 후 타임아웃
        try:
            await asyncio.wait_for(monitor_task, timeout=60.0)
        except asyncio.TimeoutError:
            logger.info("60초 테스트 완료")
        
    except Exception as e:
        logger.error(f"테스트 오류: {e}")
    finally:
        await service.stop_monitoring()
        logger.info("=== 분석 서비스 테스트 완료 ===")


async def main():
    """메인 테스트 함수"""
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()
    
    try:
        # 1. WebSocket 연결 테스트
        await test_websocket_connection()
        
        await asyncio.sleep(2)  # 잠시 대기
        
        # 2. 분석 서비스 테스트
        await test_analysis_service()
        
    except KeyboardInterrupt:
        logger.info("사용자에 의해 테스트가 중지되었습니다.")
    except Exception as e:
        logger.error(f"테스트 중 오류 발생: {e}")
    
    logger.info("모든 테스트가 완료되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
