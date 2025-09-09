import asyncio
import json
from typing import Optional, Tuple, Union

import pandas as pd
from google import genai
from google.genai.errors import ClientError
from google.genai.types import HttpOptions

from app.analysis.prompt import build_prompt, build_json_prompt
from app.analysis.models import StockAnalysisResponse
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.model_rate_limiter import ModelRateLimiter
from app.models.prompt import PromptResult
from app.models.analysis import StockInfo, StockAnalysisResult
from app.services.stock_info_service import create_stock_if_not_exists
GEMINI_TIMEOUT = 3 * 60 * 1000 # 3 minutes

class Analyzer:
    """프롬프트 생성, Gemini 실행, DB 저장을 담당하는 공통 클래스"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.get_random_key()
        self.client = genai.Client(api_key=self.api_key, http_options=HttpOptions(timeout=GEMINI_TIMEOUT))
        self.rate_limiter = ModelRateLimiter()

    async def analyze_and_save(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str,
        instrument_type: str,
        currency: str = "₩",
        unit_shares: str = "주",
        fundamental_info: Optional[dict] = None,
        position_info: Optional[dict] = None,
        minute_candles: Optional[dict] = None,
        use_json: bool = False,
    ) -> Tuple[Union[str, StockAnalysisResponse], str]:
        """
        데이터 분석, 프롬프트 생성, Gemini 실행, DB 저장을 순차적으로 수행

        Args:
            df: OHLCV 데이터
            symbol: 종목 코드/심볼
            name: 종목명
            instrument_type: 상품 유형
            currency: 통화 단위
            unit_shares: 주식/코인 단위
            fundamental_info: 기본 정보
            position_info: 보유 자산 정보
            minute_candles: 분봉 캔들 데이터 (60분, 5분, 1분)

        Returns:
            (결과 텍스트, 모델명) 튜플
        """
        # 1. 프롬프트 생성
        if use_json:
            prompt = build_json_prompt(df, symbol, name, currency, unit_shares, fundamental_info, position_info, minute_candles)
        else:
            prompt = build_prompt(df, symbol, name, currency, unit_shares, fundamental_info, position_info, minute_candles)

        # 2. Gemini 실행
        result, model_name = await self._generate_with_smart_retry(prompt, use_json=use_json)

        # 3. DB 저장
        if use_json and isinstance(result, StockAnalysisResponse):
            # JSON 분석 결과를 새로운 테이블에 저장
            await self._save_json_analysis_to_db(
                prompt, result, symbol, name, instrument_type, model_name
            )
        else:
            # 기존 텍스트 응답을 PromptResult 테이블에 저장
            result_text = str(result)
            await self._save_to_db(
                prompt, result_text, symbol, name, instrument_type, model_name
            )

        return result, model_name

    async def analyze_and_save_json(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str,
        instrument_type: str,
        currency: str = "₩",
        unit_shares: str = "주",
        fundamental_info: Optional[dict] = None,
        position_info: Optional[dict] = None,
        minute_candles: Optional[dict] = None,
    ) -> Tuple[StockAnalysisResponse, str]:
        """
        JSON 형식의 구조화된 분석 결과를 반환하는 메서드
        
        Returns:
            (StockAnalysisResponse 객체, 모델명) 튜플
        """
        return await self.analyze_and_save(
            df, symbol, name, instrument_type, currency, unit_shares,
            fundamental_info, position_info, minute_candles, use_json=True
        )

    async def close(self):
        """리소스 정리"""
        await self.rate_limiter.close()

    def __del__(self):
        """소멸자에서 리소스 정리"""
        try:
            # 비동기 메서드를 동기적으로 호출할 수 없으므로 경고만 출력
            pass
        except:
            pass

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

    async def _generate_with_smart_retry(
        self, prompt: str, max_retries: int = 3, use_json: bool = False
    ) -> Tuple[Union[str, StockAnalysisResponse], str]:
        """스마트 재시도: Redis 기반 모델 제한 + 429 에러 시 다음 모델로 즉시 전환"""

        models_to_try = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]

        for model in models_to_try:
            print(f"모델 시도: {model}")


            for attempt in range(max_retries):
                # Redis에서 현재 API 키의 모델 사용 제한 확인
                masked_api_key = self._mask_api_key(self.api_key)
                if not await self.rate_limiter.is_model_available(model, self.api_key):
                    print(f"  {model} 모델 (API: {masked_api_key}) 사용 제한 중, 다음 모델로...")
                    self.api_key = settings.get_next_key()  # 매번 첫번째껏만 받는것 같아서 랜덤으로 변경
                    continue
                try:
                    if use_json:
                        # JSON 스키마를 사용하여 구조화된 응답 생성
                        response_schema = StockAnalysisResponse.model_json_schema()
                        config = {
                            "response_mime_type": "application/json",
                            "response_schema": response_schema
                        }
                        resp = self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=config
                        )
                    else:
                        # 기존 방식 (텍스트 응답)
                        resp = self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                        )

                    if resp and resp.candidates:
                        candidate = resp.candidates[0]
                        finish_reason = getattr(candidate, "finish_reason", None)

                        if finish_reason == "STOP" and not resp.text:
                            print(f"  시도 {attempt + 1}: STOP이지만 text가 None, 재시도...")
                            await asyncio.sleep(1 + attempt)
                            continue

                        if finish_reason in ["SAFETY", "RECITATION"]:
                            print(f"  {model} 차단됨: {finish_reason}, 다음 모델로 시도...")
                            break

                        if resp.text:
                            print(f"  {model} 성공!")
                            if use_json:
                                try:
                                    # JSON 응답을 파싱하고 Pydantic 모델로 검증
                                    parsed_response = json.loads(resp.text)
                                    validated_response = StockAnalysisResponse(**parsed_response)
                                    return validated_response, model
                                except (json.JSONDecodeError, ValueError) as e:
                                    print(f"  JSON 파싱/검증 실패: {e}")
                                    if attempt < max_retries - 1:
                                        await asyncio.sleep(1 + attempt)
                                        continue
                                    else:
                                        # JSON 파싱 실패 시 텍스트 응답으로 fallback
                                        return resp.text, model
                            else:
                                return resp.text, model

                except ClientError as e:
                    if e.code == 429:
                        # 429 에러(할당량 초과) 발생 시, Redis에 모델 제한 설정
                        print(f"  {model} 모델 할당량 초과(429), Redis에 제한 설정...")

                        # retry_delay 정보 추출 및 Redis에 제한 설정
                        retry_delay = None
                        if hasattr(e, "details") and e.details:
                            try:
                                for detail in e.details.get("error", {}).get(
                                    "details", []
                                ):
                                    if (
                                        "@type" in detail
                                        and detail["@type"]
                                        == "type.googleapis.com/google.rpc.RetryInfo"
                                    ):
                                        retry_delay = detail.get("retryDelay")
                                        if retry_delay:
                                            print(f"  retry_delay: {retry_delay}")
                                            # Redis에 모델 사용 제한 설정
                                            await self.rate_limiter.set_model_rate_limit(
                                                model,
                                                self.api_key,
                                                retry_delay,
                                                e.code,
                                            )
                            except Exception as parse_error:
                                print(f"  retry_delay 파싱 오류: {parse_error}")

                        # retry_delay 정보가 없으면 기본 제한 설정
                        if not retry_delay:
                            await self.rate_limiter.set_model_rate_limit(
                                model, self.api_key, {"seconds": 60}, e.code
                            )

                        # API 키 교체 및 계속 시도
                        new_api_key = settings.get_next_key() # 매번 첫번째껏만 받는것 같아서 랜덤으로 변경
                        print(
                            f"  API 키 교체: {masked_api_key} → {self._mask_api_key(new_api_key)}"
                        )
                        self.client = genai.Client(api_key=new_api_key, http_options=HttpOptions(timeout=GEMINI_TIMEOUT))
                        self.api_key = new_api_key
                        masked_api_key = self._mask_api_key(self.api_key)

                        # 새로운 API 키로 계속 시도 (break 제거)
                        print(f"  새로운 API 키로 계속 시도...")
                        if model == "gemini-2.5-pro":
                            print("pro 는 다음 모델로 실행")
                            break
                        continue
                    else:
                        print(f"  시도 {attempt + 1} 실패 (ClientError {e.code}): {e}")
                        await asyncio.sleep(2 + attempt)

                except Exception as e:
                    print(f"  시도 {attempt + 1} 실패: {e}")

                    # 503 에러(모델 과부하) 체크
                    error_str = str(e)
                    if "503" in error_str and "UNAVAILABLE" in error_str or "500" in error_str:
                        print(f"  {model} 모델 과부하(503), Redis에 1분 제한 설정...")

                        # 503 에러 시 1분 제한 설정
                        await self.rate_limiter.set_model_rate_limit(
                            model, self.api_key, {"seconds": 60}, 503
                        )

                        # API 키 교체 및 계속 시도
                        new_api_key = settings.get_next_key()
                        masked_api_key = self._mask_api_key(self.api_key)
                        print(
                            f"  API 키 교체: {masked_api_key} → {self._mask_api_key(new_api_key)}"
                        )
                        self.client = genai.Client(api_key=new_api_key, http_options=HttpOptions(timeout=GEMINI_TIMEOUT))
                        self.api_key = new_api_key

                        # 새로운 API 키로 계속 시도
                        print(f"  새로운 API 키로 계속 시도...")
                        if model == "gemini-2.5-pro":
                            print("pro 는 다음 모델로 실행")
                            break
                        continue

                    # 그 외 다른 에러는 잠시 후 재시도
                    await asyncio.sleep(2 + attempt)
            else:
                # for 루프가 break 없이 완료되었을 때 (모든 재시도 실패)
                print(f"  {model} 모든 시도 실패, 다음 모델로...")
                continue

        return "모든 모델과 재시도 실패", "N/A"

    async def _save_to_db(
        self,
        prompt: str,
        result: Union[str, StockAnalysisResponse],
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str,
    ) -> None:
        """분석 결과를 DB에 저장"""
        async with AsyncSessionLocal() as db:
            record = PromptResult(
                prompt=prompt,
                result=result,
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
                model_name=model_name,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            print(f"DB 저장 완료: {symbol} ({name})")

    async def _save_json_analysis_to_db(
        self,
        prompt: str,
        result: StockAnalysisResponse,
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str,
    ) -> None:
        """JSON 분석 결과를 DB에 저장"""
        async with AsyncSessionLocal() as db:
            # 1. 주식 정보가 없으면 생성 (또는 기존 정보 조회)
            stock_info = await create_stock_if_not_exists(
                symbol=symbol,
                name=name,
                instrument_type=instrument_type
            )

            # 2. 근거를 JSON 문자열로 변환
            reasons_json = json.dumps(result.reasons, ensure_ascii=False)

            # 3. 분석 결과 저장
            record = StockAnalysisResult(
                stock_info_id=stock_info.id,  # 주식 정보와 연결
                prompt=prompt,
                model_name=model_name,
                decision=result.decision,
                confidence=result.confidence,
                appropriate_buy_min=result.price_analysis.appropriate_buy_range.min,
                appropriate_buy_max=result.price_analysis.appropriate_buy_range.max,
                appropriate_sell_min=result.price_analysis.appropriate_sell_range.min,
                appropriate_sell_max=result.price_analysis.appropriate_sell_range.max,
                buy_hope_min=result.price_analysis.buy_hope_range.min,
                buy_hope_max=result.price_analysis.buy_hope_range.max,
                sell_target_min=result.price_analysis.sell_target_range.min,
                sell_target_max=result.price_analysis.sell_target_range.max,
                reasons=reasons_json,
                detailed_text=result.detailed_text,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            print(f"JSON 분석 결과 DB 저장 완료: {symbol} ({name}) - StockInfo ID: {stock_info.id}")


class DataProcessor:
    """데이터 전처리를 담당하는 공통 클래스"""

    @staticmethod
    def merge_historical_and_current(
        df_historical: pd.DataFrame, df_current: pd.DataFrame
    ) -> pd.DataFrame:
        """
        과거 데이터와 현재 데이터를 병합하고 정리

        Args:
            df_historical: 과거 OHLCV 데이터
            df_current: 현재가 데이터 (1행)

        Returns:
            병합된 DataFrame
        """
        return (
            pd.concat(
                [df_historical, df_current.reset_index(drop=True)], ignore_index=True
            )
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")  # 같은 날짜면 마지막 것만 유지
            .reset_index(drop=True)
        )
