from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.analysis.analyzer as analyzer_module
from app.analysis.analyzer import Analyzer
from app.analysis.models import StockAnalysisResponse
from tests._analysis_support import (
    build_analysis_sample_df,
    build_stock_analysis_response,
)


class DummyRateLimiter:
    async def is_model_available(self, model: str, api_key: str) -> bool:
        del model, api_key
        return True

    async def set_model_rate_limit(
        self,
        model: str,
        api_key: str,
        retry_delay: object,
        error_code: int,
    ) -> None:
        del model, api_key, retry_delay, error_code
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_analyze_and_save_uses_text_prompt_and_text_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build_prompt(*args: object, **kwargs: object) -> str:
        captured["prompt_args"] = args
        captured["prompt_kwargs"] = kwargs
        return "TEXT PROMPT"

    monkeypatch.setattr(analyzer_module, "build_prompt", fake_build_prompt)
    monkeypatch.setattr(
        analyzer_module.genai,
        "Client",
        lambda *args, **kwargs: SimpleNamespace(models=SimpleNamespace()),
    )
    monkeypatch.setattr(analyzer_module, "ModelRateLimiter", DummyRateLimiter)
    monkeypatch.setattr(
        Analyzer,
        "_generate_with_smart_retry",
        AsyncMock(return_value=("TEXT RESULT", "gemini-test")),
    )
    save_text = AsyncMock()
    save_json = AsyncMock()
    monkeypatch.setattr(Analyzer, "_save_to_db", save_text)
    monkeypatch.setattr(Analyzer, "_save_json_analysis_to_db", save_json)

    analyzer = Analyzer(api_key="test-key")
    result, model_name = await analyzer.analyze_and_save(
        df=build_analysis_sample_df(),
        symbol="005930",
        name="삼성전자",
        instrument_type="equity_kr",
        fundamental_info={"PER": 12.5},
    )
    await analyzer.close()

    assert result == "TEXT RESULT"
    assert model_name == "gemini-test"
    assert captured["prompt_kwargs"] == {}
    save_text.assert_awaited_once()
    save_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyze_and_save_json_uses_json_prompt_and_structured_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build_json_prompt(*args: object, **kwargs: object) -> str:
        captured["json_prompt_args"] = args
        captured["json_prompt_kwargs"] = kwargs
        return "JSON PROMPT"

    monkeypatch.setattr(analyzer_module, "build_json_prompt", fake_build_json_prompt)
    monkeypatch.setattr(
        analyzer_module.genai,
        "Client",
        lambda *args, **kwargs: SimpleNamespace(models=SimpleNamespace()),
    )
    monkeypatch.setattr(analyzer_module, "ModelRateLimiter", DummyRateLimiter)
    monkeypatch.setattr(
        Analyzer,
        "_generate_with_smart_retry",
        AsyncMock(return_value=(build_stock_analysis_response(), "gemini-json")),
    )
    save_text = AsyncMock()
    save_json = AsyncMock()
    monkeypatch.setattr(Analyzer, "_save_to_db", save_text)
    monkeypatch.setattr(Analyzer, "_save_json_analysis_to_db", save_json)

    analyzer = Analyzer(api_key="test-key")
    result, model_name = await analyzer.analyze_and_save_json(
        df=build_analysis_sample_df(),
        symbol="AAPL",
        name="Apple",
        instrument_type="equity_us",
        fundamental_info={"PER": 28.5},
    )
    await analyzer.close()

    assert isinstance(result, StockAnalysisResponse)
    assert result.decision == "buy"
    assert model_name == "gemini-json"
    assert captured["json_prompt_kwargs"] == {}
    save_json.assert_awaited_once()
    save_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyzer_uses_text_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.analysis import analysis_repository, model_executor, prompt_builder

    calls: list[str] = []

    monkeypatch.setattr(
        prompt_builder.PromptBuilder,
        "build_text_prompt",
        lambda self, *args, **kwargs: calls.append("prompt") or "PROMPT",
    )
    monkeypatch.setattr(
        model_executor.ModelExecutor,
        "execute",
        AsyncMock(return_value=("TEXT RESULT", "gemini-collab")),
    )
    monkeypatch.setattr(
        analysis_repository.AnalysisRepository,
        "save_text_analysis",
        AsyncMock(side_effect=lambda *args, **kwargs: calls.append("save_text")),
    )
    monkeypatch.setattr(
        analyzer_module.genai,
        "Client",
        lambda *args, **kwargs: SimpleNamespace(models=SimpleNamespace()),
    )
    monkeypatch.setattr(analyzer_module, "ModelRateLimiter", DummyRateLimiter)

    analyzer = Analyzer(api_key="test-key")
    result, model_name = await analyzer.analyze_and_save(
        df=build_analysis_sample_df(),
        symbol="005930",
        name="삼성전자",
        instrument_type="equity_kr",
    )
    await analyzer.close()

    assert result == "TEXT RESULT"
    assert model_name == "gemini-collab"
    assert calls == ["prompt", "save_text"]


@pytest.mark.asyncio
async def test_analyzer_uses_json_collaborators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.analysis import analysis_repository, model_executor, prompt_builder

    calls: list[str] = []

    monkeypatch.setattr(
        prompt_builder.PromptBuilder,
        "build_json_prompt",
        lambda self, *args, **kwargs: calls.append("json_prompt") or "JSON PROMPT",
    )
    monkeypatch.setattr(
        model_executor.ModelExecutor,
        "execute",
        AsyncMock(return_value=(build_stock_analysis_response(), "gemini-json-collab")),
    )
    monkeypatch.setattr(
        analysis_repository.AnalysisRepository,
        "save_structured_analysis",
        AsyncMock(side_effect=lambda *args, **kwargs: calls.append("save_json")),
    )
    monkeypatch.setattr(
        analyzer_module.genai,
        "Client",
        lambda *args, **kwargs: SimpleNamespace(models=SimpleNamespace()),
    )
    monkeypatch.setattr(analyzer_module, "ModelRateLimiter", DummyRateLimiter)

    analyzer = Analyzer(api_key="test-key")
    result, model_name = await analyzer.analyze_and_save_json(
        df=build_analysis_sample_df(),
        symbol="AAPL",
        name="Apple",
        instrument_type="equity_us",
    )
    await analyzer.close()

    assert isinstance(result, StockAnalysisResponse)
    assert model_name == "gemini-json-collab"
    assert calls == ["json_prompt", "save_json"]


@pytest.mark.asyncio
async def test_model_executor_json_mode_uses_explicit_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.analysis.model_executor as model_executor_module
    from app.analysis.model_executor import ModelExecutor

    captured: dict[str, object] = {}
    response = MagicMock()
    response.candidates = [MagicMock(finish_reason="STOP")]
    response.text = build_stock_analysis_response().model_dump_json()
    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: captured.update(kwargs) or response
        )
    )
    monkeypatch.setattr(
        model_executor_module.genai,
        "Client",
        lambda *args, **kwargs: client,
    )
    monkeypatch.setattr(
        model_executor_module,
        "ModelRateLimiter",
        DummyRateLimiter,
    )
    executor = ModelExecutor(
        api_key="test-key",
    )

    result, model_name = await executor.execute("PROMPT", use_json=True)
    await executor.close()

    assert isinstance(result, StockAnalysisResponse)
    assert model_name.startswith("gemini")
    config = captured["config"]
    assert isinstance(config, dict)
    assert "response_json_schema" in config
    assert "response_schema" not in config
