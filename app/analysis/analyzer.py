import asyncio
from typing import Optional, Tuple
import pandas as pd
from google import genai
from google.api_core.exceptions import ResourceExhausted
from google.genai.errors import ClientError

from app.analysis.prompt import build_prompt
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.prompt import PromptResult


class Analyzer:
    """프롬프트 생성, Gemini 실행, DB 저장을 담당하는 공통 클래스"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.google_api_key
        self.client = genai.Client(api_key=self.api_key)
    
    async def analyze_and_save(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str,
        instrument_type: str,
        currency: str = "₩",
        unit_shares: str = "주"
    ) -> Tuple[str, str]:
        """
        데이터 분석, 프롬프트 생성, Gemini 실행, DB 저장을 순차적으로 수행
        
        Args:
            df: OHLCV 데이터
            symbol: 종목 코드/심볼
            name: 종목명
            instrument_type: 상품 유형
            currency: 통화 단위
            unit_shares: 주식/코인 단위
            
        Returns:
            (결과 텍스트, 모델명) 튜플
        """
        # 1. 프롬프트 생성
        prompt = build_prompt(df, symbol, name, currency, unit_shares)
        
        # 2. Gemini 실행
        result, model_name = await self._generate_with_smart_retry(prompt)
        
        # 3. DB 저장
        await self._save_to_db(prompt, result, symbol, name, instrument_type, model_name)
        
        return result, model_name
    
    async def _generate_with_smart_retry(self, prompt: str, max_retries: int = 3) -> Tuple[str, str]:
        """스마트 재시도: 429 에러 시 다음 모델로 즉시 전환"""
        
        models_to_try = ["gemini-2.5-pro", "gemini-2.5-flash"]
        
        for model in models_to_try:
            print(f"모델 시도: {model}")
            
            for attempt in range(max_retries):
                try:
                    resp = self.client.models.generate_content(
                        model=model,
                        contents=prompt,
                    )
                    
                    if resp and resp.candidates:
                        candidate = resp.candidates[0]
                        finish_reason = getattr(candidate, 'finish_reason', None)
                        
                        if finish_reason == "STOP" and not resp.text:
                            print(f"  시도 {attempt + 1}: STOP이지만 text가 None, 재시도...")
                            await asyncio.sleep(1 + attempt)
                            continue
                        
                        if finish_reason in ["SAFETY", "RECITATION"]:
                            print(f"  {model} 차단됨: {finish_reason}, 다음 모델로 시도...")
                            break
                        
                        if resp.text:
                            print(f"  {model} 성공!")
                            return resp.text, model
                            
                except ClientError as e:
                    if e.code == 429:
                        # 429 에러(할당량 초과) 발생 시, 해당 모델 재시도 없이 바로 다음 모델로 넘어감
                        print(f"  {model} 모델 할당량 초과(429), 다음 모델로 시도...")
                        self.client = genai.Client(api_key=settings.get_next_key())  # API 키 교체
                        for detail in e.details["error"]["details"]:
                            if "@type" in detail and detail["@type"] == "type.googleapis.com/google.rpc.RetryInfo":
                                retry_delay = detail.get("retryDelay")
                                if retry_delay:
                                    print(retry_delay)
                        break  # attempt 루프를 탈출하여 다음 model로 넘어감
                    else:
                        print(f"  시도 {attempt + 1} 실패 (ClientError {e.code}): {e}")
                        await asyncio.sleep(2 + attempt)



                except Exception as e:
                    print(f"  시도 {attempt + 1} 실패: {e}")
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
        result: str,
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str
    ) -> None:
        """분석 결과를 DB에 저장"""
        async with AsyncSessionLocal() as db:
            record = PromptResult(
                prompt=prompt,
                result=result,
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
                model_name=model_name
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            print(f"DB 저장 완료: {symbol} ({name})")


class DataProcessor:
    """데이터 전처리를 담당하는 공통 클래스"""
    
    @staticmethod
    def merge_historical_and_current(
        df_historical: pd.DataFrame,
        df_current: pd.DataFrame
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
            pd.concat([df_historical, df_current.reset_index(drop=True)], ignore_index=True)
            .sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")  # 같은 날짜면 마지막 것만 유지
            .reset_index(drop=True)
        )
