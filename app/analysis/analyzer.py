
import pandas as pd
from google import genai
from google.genai.types import HttpOptions

from app.analysis.analysis_repository import AnalysisRepository
from app.analysis.model_executor import GEMINI_TIMEOUT, ModelExecutor
from app.analysis.models import StockAnalysisResponse
from app.analysis.prompt import build_json_prompt, build_prompt
from app.analysis.prompt_builder import PromptBuilder
from app.analysis.response_validator import ResponseValidator
from app.core.config import settings
from app.core.model_rate_limiter import ModelRateLimiter

# from app.services.stock_info_service import create_stock_if_not_exists


class Analyzer:
    """프롬프트 생성, Gemini 실행, DB 저장을 담당하는 공통 클래스"""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.get_random_key()
        self.client = genai.Client(
            api_key=self.api_key, http_options=HttpOptions(timeout=GEMINI_TIMEOUT)
        )
        self.rate_limiter = ModelRateLimiter()
        self.response_validator = ResponseValidator()
        self.prompt_builder = PromptBuilder(
            text_builder=build_prompt,
            json_builder=build_json_prompt,
        )
        self.model_executor = ModelExecutor(
            api_key=self.api_key,
            client=self.client,
            rate_limiter=self.rate_limiter,
            validator=self.response_validator,
        )
        self.repository = AnalysisRepository()

    async def analyze_and_save(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str,
        instrument_type: str,
        currency: str = "₩",
        unit_shares: str = "주",
        fundamental_info: dict | None = None,
        position_info: dict | None = None,
        minute_candles: dict | None = None,
        use_json: bool = False,
    ) -> tuple[str | StockAnalysisResponse, str]:
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
            prompt = self.prompt_builder.build_json_prompt(
                df,
                symbol,
                name,
                instrument_type,
                currency=currency,
                unit_shares=unit_shares,
                fundamental_info=fundamental_info,
                position_info=position_info,
                minute_candles=minute_candles,
            )
        else:
            prompt = self.prompt_builder.build_text_prompt(
                df,
                symbol,
                name,
                instrument_type,
                currency=currency,
                unit_shares=unit_shares,
                fundamental_info=fundamental_info,
                position_info=position_info,
                minute_candles=minute_candles,
            )

        # 2. Gemini 실행
        result, model_name = await self._generate_with_smart_retry(
            prompt, use_json=use_json
        )

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
        fundamental_info: dict | None = None,
        position_info: dict | None = None,
        minute_candles: dict | None = None,
    ) -> tuple[str | StockAnalysisResponse, str]:
        """
        JSON 형식의 구조화된 분석 결과를 반환하는 메서드

        Returns:
            (StockAnalysisResponse 객체, 모델명) 튜플
        """
        return await self.analyze_and_save(
            df,
            symbol,
            name,
            instrument_type,
            currency,
            unit_shares,
            fundamental_info,
            position_info,
            minute_candles,
            use_json=True,
        )

    async def close(self):
        """리소스 정리"""
        await self.model_executor.close()

    def __del__(self):
        """소멸자에서 리소스 정리"""
        try:
            # 비동기 메서드를 동기적으로 호출할 수 없으므로 경고만 출력
            pass
        except Exception:
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
    ) -> tuple[str | StockAnalysisResponse, str]:
        result, model_name = await self.model_executor.execute(
            prompt,
            max_retries=max_retries,
            use_json=use_json,
        )
        self.api_key = self.model_executor.api_key
        self.client = self.model_executor.client
        return result, model_name

    async def _save_to_db(
        self,
        prompt: str,
        result: str | StockAnalysisResponse,
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str,
    ) -> None:
        await self.repository.save_text_analysis(
            prompt,
            result,
            symbol,
            name,
            instrument_type,
            model_name,
        )

    async def _save_json_analysis_to_db(
        self,
        prompt: str,
        result: StockAnalysisResponse,
        symbol: str,
        name: str,
        instrument_type: str,
        model_name: str,
    ) -> None:
        await self.repository.save_structured_analysis(
            prompt,
            result,
            symbol,
            name,
            instrument_type,
            model_name,
        )


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
            .drop_duplicates(
                subset=["date"], keep="last"
            )  # 같은 날짜면 마지막 것만 유지
            .reset_index(drop=True)
        )
