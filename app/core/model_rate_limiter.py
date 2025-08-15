import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import redis.asyncio as redis

from app.core.config import settings


class ModelRateLimiter:
    """Redis를 활용한 모델 사용 제한 관리자"""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or settings.get_redis_url()
        self.redis_client: Optional[redis.Redis] = None
        self._rate_limit_key_prefix = "model_rate_limit:"
        self._retry_info_key_prefix = "model_retry_info:"

    async def _get_redis_client(self) -> redis.Redis:
        """Redis 클라이언트를 가져오거나 생성"""
        if self.redis_client is None:
            # 연결 풀 설정을 포함한 Redis 클라이언트 생성
            self.redis_client = redis.from_url(
                self.redis_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_connect_timeout,
                decode_responses=True,  # 문자열 응답을 자동으로 디코딩
            )
        return self.redis_client

    async def close(self):
        """Redis 연결 종료"""
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None

    async def is_model_available(self, model_name: str, api_key: str) -> bool:
        """
        특정 API 키의 모델이 사용 가능한지 확인

        Args:
            model_name: 모델명 (예: "gemini-2.5-pro")
            api_key: Gemini API 키 (마스킹된 형태)

        Returns:
            사용 가능 여부
        """
        try:
            redis_client = await self._get_redis_client()
            key = f"{self._rate_limit_key_prefix}{model_name}:{api_key}"

            # 제한 정보가 있는지 확인
            rate_limit_data = await redis_client.get(key)
            if not rate_limit_data:
                return True  # 제한 정보가 없으면 사용 가능

            # 제한 정보 파싱
            limit_info = json.loads(rate_limit_data)
            until_time = datetime.fromisoformat(limit_info["until"])

            # 현재 시간이 제한 시간을 지났으면 제한 해제
            if datetime.now() > until_time:
                await redis_client.delete(key)
                return True

            # 아직 제한 시간 내
            remaining_time = until_time - datetime.now()
            print(
                f"  {model_name} 모델 (API: {api_key}) 사용 제한 중. 남은 시간: {remaining_time}"
            )
            return False

        except Exception as e:
            print(f"Redis 연결 오류: {e}")
            return True  # Redis 오류 시 제한 없이 사용

    async def set_model_rate_limit(
        self,
        model_name: str,
        api_key: str,
        retry_delay: Dict[str, Any],
        error_code: int = 429,
    ) -> None:
        """
        특정 API 키의 모델 사용 제한을 설정

        Args:
            model_name: 모델명
            api_key: Gemini API 키 (마스킹된 형태)
            retry_delay: Google API에서 받은 retry_delay 정보
            error_code: 에러 코드 (기본값: 429)
        """
        try:
            redis_client = await self._get_redis_client()

            # retry_delay에서 seconds 추출
            seconds = self._extract_retry_seconds(retry_delay)
            if seconds is None:
                # retry_delay 정보가 없으면 기본값 사용
                seconds = 60  # 1분

            # 제한 종료 시간 계산
            until_time = datetime.now() + timedelta(seconds=seconds)

            # API 키별로 제한 정보를 Redis에 저장
            key = f"{self._rate_limit_key_prefix}{model_name}:{api_key}"
            limit_info = {
                "model": model_name,
                "api_key": api_key,
                "error_code": error_code,
                "until": until_time.isoformat(),
                "retry_delay": retry_delay,
                "set_at": datetime.now().isoformat(),
            }

            # TTL도 함께 설정 (제한 시간 + 여유분)
            ttl_seconds = seconds + 10

            await redis_client.setex(key, ttl_seconds, json.dumps(limit_info))

            print(
                f"  {model_name} 모델 (API: {api_key}) 사용 제한 설정: {seconds}초 ({until_time.strftime('%H:%M:%S')}까지)"
            )

            # 재시도 정보도 저장 (디버깅용)
            retry_key = f"{self._retry_info_key_prefix}{model_name}:{api_key}"
            await redis_client.setex(
                retry_key,
                ttl_seconds,
                json.dumps(
                    {
                        "model": model_name,
                        "api_key": api_key,
                        "retry_delay": retry_delay,
                        "error_code": error_code,
                        "set_at": datetime.now().isoformat(),
                    }
                ),
            )

        except Exception as e:
            print(f"Redis 제한 설정 오류: {e}")

    def _extract_retry_seconds(self, retry_delay: Dict[str, Any]) -> Optional[int]:
        """
        retry_delay에서 seconds 값을 추출

        Args:
            retry_delay: Google API retry_delay 정보

        Returns:
            초 단위 시간 또는 None
        """
        try:
            # Google API retry_delay 구조에 따라 seconds 추출
            if "seconds" in retry_delay:
                return int(retry_delay["seconds"])
            elif "nanos" in retry_delay:
                # nanos가 있는 경우 seconds로 변환
                return int(retry_delay.get("seconds", 0)) + (
                    int(retry_delay["nanos"]) // 1_000_000_000
                )
            else:
                # 기본 구조 확인
                print(f"  retry_delay 구조: {retry_delay}")
                return None
        except Exception as e:
            print(f"  retry_delay 파싱 오류: {e}")
            return None

    async def get_model_status(
        self, model_name: str, api_key: str = None
    ) -> Dict[str, Any]:
        """
        모델의 현재 상태 정보 조회 (API 키별 또는 전체)

        Args:
            model_name: 모델명
            api_key: 특정 API 키 (None이면 해당 모델의 모든 API 키 상태 조회)

        Returns:
            모델 상태 정보
        """
        try:
            redis_client = await self._get_redis_client()

            if api_key:
                # 특정 API 키의 상태만 조회
                key = f"{self._rate_limit_key_prefix}{model_name}:{api_key}"
                rate_limit_data = await redis_client.get(key)

                if not rate_limit_data:
                    return {
                        "status": "available",
                        "model": model_name,
                        "api_key": api_key,
                    }

                limit_info = json.loads(rate_limit_data)
                until_time = datetime.fromisoformat(limit_info["until"])
                remaining_time = until_time - datetime.now()

                if remaining_time.total_seconds() > 0:
                    return {
                        "status": "rate_limited",
                        "model": model_name,
                        "api_key": api_key,
                        "until": limit_info["until"],
                        "remaining_seconds": int(remaining_time.total_seconds()),
                        "error_code": limit_info.get("error_code"),
                        "retry_delay": limit_info.get("retry_delay"),
                    }
                else:
                    return {
                        "status": "available",
                        "model": model_name,
                        "api_key": api_key,
                    }
            else:
                # 해당 모델의 모든 API 키 상태 조회
                pattern = f"{self._rate_limit_key_prefix}{model_name}:*"
                keys = await redis_client.keys(pattern)

                if not keys:
                    return {"status": "available", "model": model_name, "api_keys": []}

                api_key_statuses = []
                for key in keys:
                    rate_limit_data = await redis_client.get(key)
                    if rate_limit_data:
                        limit_info = json.loads(rate_limit_data)
                        api_key_part = key.split(":", 2)[
                            2
                        ]  # model_name:api_key에서 api_key 부분
                        until_time = datetime.fromisoformat(limit_info["until"])
                        remaining_time = until_time - datetime.now()

                        if remaining_time.total_seconds() > 0:
                            api_key_statuses.append(
                                {
                                    "api_key": api_key_part,
                                    "status": "rate_limited",
                                    "until": limit_info["until"],
                                    "remaining_seconds": int(
                                        remaining_time.total_seconds()
                                    ),
                                    "error_code": limit_info.get("error_code"),
                                }
                            )
                        else:
                            api_key_statuses.append(
                                {"api_key": api_key_part, "status": "available"}
                            )

                return {
                    "status": "mixed",
                    "model": model_name,
                    "api_keys": api_key_statuses,
                }

        except Exception as e:
            print(f"모델 상태 조회 오류: {e}")
            return {"status": "error", "model": model_name, "error": str(e)}

    async def clear_model_rate_limit(
        self, model_name: str, api_key: str = None
    ) -> bool:
        """
        모델의 사용 제한을 수동으로 해제 (API 키별 또는 전체)

        Args:
            model_name: 모델명
            api_key: 특정 API 키 (None이면 해당 모델의 모든 API 키 제한 해제)

        Returns:
            해제 성공 여부
        """
        try:
            redis_client = await self._get_redis_client()

            if api_key:
                # 특정 API 키의 제한만 해제
                key = f"{self._rate_limit_key_prefix}{model_name}:{api_key}"
                retry_key = f"{self._retry_info_key_prefix}{model_name}:{api_key}"

                await redis_client.delete(key)
                await redis_client.delete(retry_key)

                print(f"  {model_name} 모델 (API: {api_key}) 사용 제한 수동 해제")
                return True
            else:
                # 해당 모델의 모든 API 키 제한 해제
                pattern = f"{self._rate_limit_key_prefix}{model_name}:*"
                keys = await redis_client.keys(pattern)

                if not keys:
                    print(f"  {model_name} 모델에 제한된 API 키가 없습니다.")
                    return True

                deleted_count = 0
                for key in keys:
                    await redis_client.delete(key)
                    # retry_info 키도 함께 삭제
                    retry_key = key.replace(
                        self._rate_limit_key_prefix, self._retry_info_key_prefix
                    )
                    await redis_client.delete(retry_key)
                    deleted_count += 1

                print(f"  {model_name} 모델의 {deleted_count}개 API 키 제한 해제 완료")
                return True

        except Exception as e:
            print(f"모델 제한 해제 오류: {e}")
            return False

    def _mask_api_key(self, api_key: str) -> str:
        """
        API 키를 마스킹하여 보안 강화

        Args:
            api_key: 원본 API 키

        Returns:
            마스킹된 API 키 (예: "AIza...abc123" -> "AIza...***")
        """
        if not api_key or len(api_key) < 8:
            return "***"

        # 앞 4글자와 뒤 3글자만 보이고 나머지는 ***
        return f"{api_key[:4]}...{api_key[-3:]}"

    async def get_all_rate_limits(self) -> Dict[str, Any]:
        """
        모든 모델과 API 키의 제한 상태를 조회

        Returns:
            전체 제한 상태 정보
        """
        try:
            redis_client = await self._get_redis_client()
            pattern = f"{self._rate_limit_key_prefix}*"
            keys = await redis_client.keys(pattern)

            if not keys:
                return {"total_limited": 0, "models": {}}

            all_status = {}
            total_limited = 0

            for key in keys:
                # key 형식: "model_rate_limit:model_name:api_key"
                parts = key.split(":", 2)
                if len(parts) == 3:
                    model_name, api_key = parts[1], parts[2]

                    if model_name not in all_status:
                        all_status[model_name] = {"api_keys": []}

                    rate_limit_data = await redis_client.get(key)
                    if rate_limit_data:
                        limit_info = json.loads(rate_limit_data)
                        until_time = datetime.fromisoformat(limit_info["until"])
                        remaining_time = until_time - datetime.now()

                        if remaining_time.total_seconds() > 0:
                            all_status[model_name]["api_keys"].append(
                                {
                                    "api_key": self._mask_api_key(api_key),
                                    "status": "rate_limited",
                                    "until": limit_info["until"],
                                    "remaining_seconds": int(
                                        remaining_time.total_seconds()
                                    ),
                                    "error_code": limit_info.get("error_code"),
                                }
                            )
                            total_limited += 1
                        else:
                            all_status[model_name]["api_keys"].append(
                                {
                                    "api_key": self._mask_api_key(api_key),
                                    "status": "available",
                                }
                            )

            return {"total_limited": total_limited, "models": all_status}

        except Exception as e:
            print(f"전체 제한 상태 조회 오류: {e}")
            return {"error": str(e)}
