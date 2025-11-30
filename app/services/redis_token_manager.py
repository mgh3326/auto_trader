import asyncio
import json
import logging
import os
import time
from typing import Optional

import redis.asyncio as redis

from app.core.config import settings


class RedisTokenManager:
    """Redis 기반 토큰 관리 서비스 with 분산 락"""

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self._lock_key = "kis:token:lock"
        self._token_key = "kis:access_token"
        self._lock_timeout = 30  # 락 타임아웃 (초)
        self._token_expiry_buffer = 60  # 토큰 만료 전 버퍼 (초)
        self._current_lock_value: Optional[str] = None  # 현재 획득한 락 값 저장
    
    async def _get_redis_client(self) -> redis.Redis:
        """Redis 클라이언트 가져오기 (지연 초기화)"""
        if self.redis_client is None:
            redis_url = settings.get_redis_url()
            self.redis_client = redis.from_url(
                redis_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True
            )
        return self.redis_client
    
    async def _acquire_lock(self) -> bool:
        """분산 락 획득 (더 강력한 버전)"""
        redis_client = await self._get_redis_client()
        lock_value = f"{time.time()}:{id(self)}:{os.getpid()}"

        # SET with NX and EX 옵션으로 원자적 락 설정
        result = await redis_client.set(
            self._lock_key,
            lock_value,
            nx=True,
            ex=self._lock_timeout
        )

        if result:
            # 락 획득 성공 시 값 확인 (다른 프로세스가 이미 락을 가졌는지 체크)
            current_value = await redis_client.get(self._lock_key)
            if current_value == lock_value:
                self._current_lock_value = lock_value  # 락 값 저장
                return True
            else:
                # 다른 프로세스가 락을 가짐
                return False

        return False
    
    async def _release_lock(self) -> None:
        """분산 락 해제 (안전한 버전)"""
        if not self._current_lock_value:
            logging.warning("해제할 락 값이 없음")
            return

        redis_client = await self._get_redis_client()

        # Lua 스크립트로 원자적 락 해제 (본인이 설정한 락만 해제)
        lua_script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        else
            return 0
        end
        """

        try:
            await redis_client.eval(
                lua_script, 1, self._lock_key, self._current_lock_value
            )
        except Exception as e:
            logging.warning(f"락 해제 중 오류 (무시됨): {e}")
            # 락 해제 실패해도 계속 진행 (TTL로 자동 해제됨)
        finally:
            self._current_lock_value = None  # 락 값 초기화
    
    def _is_token_valid(self, token_data: dict) -> bool:
        """토큰이 유효한지 확인 (만료 시간 체크)"""
        if not token_data or "expires_at" not in token_data:
            return False
        
        current_time = time.time()
        expires_at = token_data["expires_at"]
        
        # 버퍼 시간을 고려하여 만료 여부 판단
        return current_time < (expires_at - self._token_expiry_buffer)
    
    async def get_token(self) -> Optional[str]:
        """Redis에서 토큰 가져오기"""
        try:
            redis_client = await self._get_redis_client()
            token_data_str = await redis_client.get(self._token_key)
            
            if not token_data_str:
                logging.info("Redis에 토큰이 없음")
                return None
            
            token_data = json.loads(token_data_str)
            
            if self._is_token_valid(token_data):
                logging.info("Redis에서 유효한 토큰 사용")
                return token_data["access_token"]
            else:
                logging.info("Redis의 토큰이 만료됨")
                return None
                
        except Exception as e:
            logging.error(f"Redis에서 토큰 조회 실패: {e}")
            return None
    
    async def save_token(self, access_token: str, expires_in: int = 3600) -> None:
        """Redis에 토큰 저장"""
        try:
            redis_client = await self._get_redis_client()
            
            token_data = {
                "access_token": access_token,
                "expires_at": time.time() + expires_in,
                "created_at": time.time()
            }
            
            # 토큰을 JSON으로 직렬화하여 저장
            await redis_client.set(
                self._token_key, 
                json.dumps(token_data),
                ex=expires_in + self._token_expiry_buffer  # Redis TTL도 설정
            )
            
            logging.info(f"Redis에 토큰 저장 완료 (만료: {expires_in}초)")
            
        except Exception as e:
            logging.error(f"Redis에 토큰 저장 실패: {e}")
    
    async def refresh_token_with_lock(self, token_fetcher) -> str:
        """
        분산 락을 사용하여 토큰 새로 발급
        
        Args:
            token_fetcher: 토큰을 새로 발급하는 async 함수 (access_token, expires_in 반환)
            
        Returns:
            str: 새로 발급된 access_token
        """
        # 먼저 기존 토큰 확인 (여러 번 체크)
        for attempt in range(3):
            existing_token = await self.get_token()
            if existing_token:
                logging.info(f"기존 토큰 사용: {existing_token[:10]}...")
                return existing_token
            if attempt < 2:  # 마지막 시도가 아니면 잠시 대기
                await asyncio.sleep(0.05)
        
        # 락 획득 시도
        logging.info("토큰 발급을 위한 분산 락 획득 시도...")
        lock_acquired = await self._acquire_lock()
        if not lock_acquired:
            logging.info("락 획득 실패, 대기 중...")
            # 락 획득 실패 시 더 긴 대기
            await asyncio.sleep(0.2)
            existing_token = await self.get_token()
            if existing_token:
                logging.info(f"대기 중 다른 프로세스가 토큰 발급: {existing_token[:10]}...")
                return existing_token
            
            # 여전히 토큰이 없으면 락 대기 (더 긴 대기)
            for i in range(30):  # 3초 대기 (0.1초 * 30)
                await asyncio.sleep(0.1)
                existing_token = await self.get_token()
                if existing_token:
                    logging.info(f"대기 중 토큰 발견: {existing_token[:10]}...")
                    return existing_token
                if i % 10 == 0:  # 1초마다 로그
                    logging.info(f"토큰 발급 대기 중... ({i/10 + 1}초)")
            
            raise RuntimeError("토큰 발급 락 획득 실패")
        
        try:
            logging.info("분산 락 획득 성공, 토큰 발급 시작")
            # 락을 획득한 상태에서 다시 한번 확인
            existing_token = await self.get_token()
            if existing_token:
                logging.info(f"락 획득 후 기존 토큰 발견: {existing_token[:10]}...")
                return existing_token
            
            # 새 토큰 발급
            logging.info("새 KIS 토큰 발급 시작")
            access_token, expires_in = await token_fetcher()
            
            # Redis에 저장
            await self.save_token(access_token, expires_in)
            logging.info(f"토큰 발급 및 Redis 저장 완료: {access_token[:10]}...")
            
            return access_token
            
        finally:
            # 락 해제
            logging.info("분산 락 해제")
            await self._release_lock()
    
    async def clear_token(self) -> None:
        """Redis에서 토큰 삭제"""
        try:
            redis_client = await self._get_redis_client()
            await redis_client.delete(self._token_key)
            logging.info("Redis에서 토큰 삭제 완료")
        except Exception as e:
            logging.error(f"Redis에서 토큰 삭제 실패: {e}")
    
    async def close(self) -> None:
        """Redis 연결 종료"""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None


# 전역 인스턴스
redis_token_manager = RedisTokenManager()
