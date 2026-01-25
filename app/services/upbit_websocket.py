"""업비트 WebSocket 내 주문 및 체결 (MyOrder) 클라이언트"""

import asyncio
import json
import logging
import ssl
import uuid
from collections.abc import Callable

import jwt
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings
from data.coins_info import upbit_pairs

# 로깅 설정
logger = logging.getLogger(__name__)


class UpbitMyOrderWebSocket:
    """업비트 내 주문 및 체결 WebSocket 클라이언트"""

    def __init__(
        self, on_order_callback: Callable | None = None, verify_ssl: bool = False
    ):
        """
        Args:
            on_order_callback: 주문/체결 데이터 수신 시 호출될 콜백 함수
                              함수 시그니처: async def callback(order_data: dict) -> None
            verify_ssl: SSL 인증서 검증 여부 (기본값: False - macOS 호환성)
        """
        self.websocket_url = "wss://api.upbit.com/websocket/v1/private"
        self.on_order_callback = on_order_callback
        self.verify_ssl = verify_ssl
        self.websocket = None
        self.is_connected = False
        self.reconnect_delay = 5  # 재연결 대기 시간 (초)
        self.max_reconnect_attempts = 10
        self.current_attempt = 0

    def _create_ssl_context(self):
        """SSL 컨텍스트 생성"""
        if self.verify_ssl:
            # 정상적인 SSL 검증 사용
            ssl_context = ssl.create_default_context()
            logger.info("SSL 인증서 검증을 사용합니다.")
        else:
            # SSL 검증 비활성화 (macOS 호환성)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            logger.info("SSL 인증서 검증을 비활성화했습니다. (macOS 호환성)")

        return ssl_context

    def _create_auth_token(self):
        """JWT 인증 토큰 생성"""
        try:
            payload = {
                "access_key": settings.upbit_access_key,
                "nonce": str(uuid.uuid4()),
            }

            jwt_token = jwt.encode(
                payload, settings.upbit_secret_key, algorithm="HS256"
            )
            return jwt_token
        except Exception as e:
            logger.error(f"JWT 토큰 생성 실패: {e}")
            return None

    async def connect_and_subscribe(self, coin_pairs: list | None = None):
        """
        WebSocket 연결 및 구독 시작

        Args:
            coin_pairs: 구독할 코인 페어 리스트 (예: ["KRW-BTC", "KRW-ETH"])
                       None이면 모든 페어 구독
        """
        while self.current_attempt < self.max_reconnect_attempts:
            try:
                await self._connect_and_subscribe_internal(coin_pairs)
                # 성공적으로 연결된 경우 재시도 카운터 초기화
                self.current_attempt = 0
                break
            except Exception as e:
                self.current_attempt += 1
                logger.error(
                    f"WebSocket 연결 실패 (시도 {self.current_attempt}/{self.max_reconnect_attempts}): {e}"
                )

                if self.current_attempt >= self.max_reconnect_attempts:
                    logger.error(
                        "최대 재연결 시도 횟수에 도달했습니다. 연결을 중단합니다."
                    )
                    break

                logger.info(f"{self.reconnect_delay}초 후 재연결을 시도합니다...")
                await asyncio.sleep(self.reconnect_delay)

    async def _connect_and_subscribe_internal(self, coin_pairs: list | None = None):
        """내부 연결 및 구독 로직"""
        logger.info("업비트 MyOrder WebSocket 연결을 시작합니다...")

        # SSL 컨텍스트 설정 (인증서 검증 문제 해결)
        ssl_context = self._create_ssl_context()

        # JWT 인증 토큰 생성
        auth_token = self._create_auth_token()
        if not auth_token:
            raise Exception("JWT 인증 토큰 생성에 실패했습니다.")

        # WebSocket 연결 (헤더 인증 방식)
        # websockets 15+ 버전에서는 create_connection을 직접 사용해야 함
        import websockets.legacy.client

        headers = {"Authorization": f"Bearer {auth_token}"}

        self.websocket = await websockets.legacy.client.connect(
            self.websocket_url,
            ssl=ssl_context,
            extra_headers=headers,  # 헤더 방식 인증
            ping_interval=30,
            ping_timeout=10,
            close_timeout=10,
        )

        logger.info("헤더 방식으로 인증 성공")

        # 인증 토큰을 저장해서 구독 메시지에서 사용
        self.auth_token = auth_token

        self.is_connected = True
        logger.info("WebSocket 연결 성공")

        # 구독 메시지 생성 및 전송 (인증 정보 포함)
        subscribe_message = self._create_subscribe_message(coin_pairs)
        await self.websocket.send(json.dumps(subscribe_message))
        logger.info("구독 메시지 전송 완료")

        # 메시지 수신 루프 시작
        await self._listen_for_messages()

    def _create_subscribe_message(self, coin_pairs: list | None = None) -> list:
        """구독 메시지 생성"""
        # 기본 메시지 구조
        message = [
            {
                "ticket": str(uuid.uuid4())  # 고유 티켓 ID
            },
            {"type": "myOrder"},
        ]

        # 특정 코인 페어 지정 시
        if coin_pairs:
            message[1]["codes"] = coin_pairs
            logger.info(f"특정 코인 페어 구독: {coin_pairs}")
        else:
            message[1]["codes"] = []  # 모든 페어 구독
            logger.info("모든 코인 페어 구독")

        return message

    async def _listen_for_messages(self):
        """메시지 수신 루프"""
        logger.info("메시지 수신 대기 중...")
        if self.websocket is None:
            logger.error("WebSocket이 연결되지 않았습니다.")
            return
        try:
            async for message in self.websocket:
                try:
                    # 원본 메시지 로깅 (디버깅용)
                    logger.debug(f"원본 메시지 수신: {message}")

                    # JSON 데이터 파싱
                    if isinstance(message, bytes):
                        message = message.decode("utf-8")

                    data = json.loads(message)
                    logger.info(f"메시지 파싱 성공: {data}")

                    # 에러 메시지 확인
                    if "error" in data:
                        logger.error(f"서버 에러 메시지: {data['error']}")
                        continue

                    # MyOrder 데이터인지 확인
                    if data.get("type") == "myOrder":
                        logger.info(
                            f"주문/체결 데이터 수신: {data.get('code')} - {data.get('state')}"
                        )

                        # 콜백 함수 호출
                        if self.on_order_callback:
                            await self.on_order_callback(data)
                        else:
                            logger.warning("콜백 함수가 설정되지 않았습니다.")
                    else:
                        logger.info(
                            f"다른 타입의 메시지: {data.get('type', '알 수 없음')}"
                        )

                except json.JSONDecodeError as e:
                    logger.error(f"JSON 파싱 오류: {e}, 원본 메시지: {message}")
                except Exception as e:
                    logger.error(f"메시지 처리 오류: {e}, 원본 메시지: {message}")

        except ConnectionClosed:
            logger.warning("WebSocket 연결이 종료되었습니다.")
            self.is_connected = False
        except WebSocketException as e:
            logger.error(f"WebSocket 오류: {e}")
            self.is_connected = False
        except Exception as e:
            logger.error(f"예상하지 못한 오류: {e}")
            self.is_connected = False

    async def disconnect(self):
        """WebSocket 연결 종료"""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("WebSocket 연결을 종료했습니다.")

    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """비동기 컨텍스트 매니저 종료"""
        await self.disconnect()


class UpbitOrderAnalysisService:
    """업비트 주문/체결 데이터 기반 자동 분석 서비스"""

    def __init__(
        self, analyzer_callback: Callable | None = None, verify_ssl: bool = False
    ):
        """
        Args:
            analyzer_callback: 분석 수행 콜백 함수
                              함수 시그니처: async def callback(coin_name: str) -> None
            verify_ssl: SSL 인증서 검증 여부 (기본값: False - macOS 호환성)
        """
        self.analyzer_callback = analyzer_callback
        self.verify_ssl = verify_ssl
        self.websocket_client = None
        self.is_running = False

    async def start_monitoring(self, coin_pairs: list | None = None):
        """모니터링 시작"""
        if self.is_running:
            logger.warning("이미 모니터링이 실행 중입니다.")
            return

        self.is_running = True
        logger.info("업비트 주문/체결 모니터링을 시작합니다...")

        # WebSocket 클라이언트 생성
        self.websocket_client = UpbitMyOrderWebSocket(
            on_order_callback=self._handle_order_data, verify_ssl=self.verify_ssl
        )

        try:
            # 연결 및 구독 시작 (재연결 포함)
            await self.websocket_client.connect_and_subscribe(coin_pairs)
        except Exception as e:
            logger.error(f"모니터링 시작 오류: {e}")
            self.is_running = False
            raise

    async def stop_monitoring(self):
        """모니터링 중지"""
        if not self.is_running:
            logger.warning("모니터링이 실행되지 않고 있습니다.")
            return

        self.is_running = False
        logger.info("업비트 주문/체결 모니터링을 중지합니다...")

        if self.websocket_client:
            await self.websocket_client.disconnect()
            self.websocket_client = None

    async def _handle_order_data(self, order_data: dict):
        """주문/체결 데이터 처리"""
        try:
            # 체결 상태인 경우에만 분석 수행
            state = order_data.get("state")
            if state != "trade":
                logger.debug(f"체결이 아닌 상태이므로 무시: {state}")
                return

            # 코인 페어에서 코인명 추출
            code = order_data.get("code")  # 예: "KRW-BTC"
            if not code or not code.startswith("KRW-"):
                logger.warning(f"지원하지 않는 코인 페어: {code}")
                return

            # 페어를 한국 이름으로 변환
            await upbit_pairs.prime_upbit_constants()
            coin_name = upbit_pairs.PAIR_TO_NAME_KR.get(code)

            if not coin_name:
                logger.warning(f"코인명을 찾을 수 없음: {code}")
                return

            # 매수/매도 구분
            ask_bid = order_data.get("ask_bid")  # "ASK"(매도) 또는 "BID"(매수)
            trade_volume = order_data.get("executed_volume", 0)
            trade_price = order_data.get("price", 0)

            logger.info(
                f"체결 감지 - {coin_name}({code}): {ask_bid} {trade_volume}개 @ {trade_price}원"
            )

            # 분석 콜백 호출
            if self.analyzer_callback:
                logger.info(f"{coin_name} 분석을 시작합니다...")
                await self.analyzer_callback(coin_name)
                logger.info(f"{coin_name} 분석이 완료되었습니다.")
            else:
                logger.warning("분석 콜백이 설정되지 않았습니다.")

        except Exception as e:
            logger.error(f"주문 데이터 처리 오류: {e}")
            logger.error(f"문제가 된 데이터: {order_data}")

    async def __aenter__(self):
        """비동기 컨텍스트 매니저 진입"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """비동기 컨텍스트 매니저 종료"""
        await self.stop_monitoring()
