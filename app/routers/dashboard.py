# app/routers/dashboard.py
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.prompt import PromptResult
from app.models.trading import InstrumentType

# templates 폴더를 프로젝트 루트(api 코드와 같은 레벨)에 둔다고 가정
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """
    간단한 대시보드 홈. 추후 Jinja → React/Vue SPA 교체 가능.
    """
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "KIS Auto Screener",
        },
    )


@router.get("/analysis", response_class=HTMLResponse)
async def analysis_list(request: Request):
    """
    분석 결과 리스트 페이지
    """
    return templates.TemplateResponse(
        "analysis_list.html",
        {
            "request": request,
            "title": "분석 결과",
        },
    )


@router.get("/api/analysis/symbols")
async def get_unique_symbols(
    instrument_type: str | None = None, db: AsyncSession = Depends(get_db)
):
    """
    고유한 종목 코드 목록을 반환
    """
    print(f"Symbols API 호출됨 - instrument_type: {instrument_type}")

    try:
        query = select(PromptResult.symbol).distinct()

        if instrument_type and instrument_type.strip():
            print(f"필터링 적용: {instrument_type}")
            try:
                if instrument_type == "equity_kr":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_kr
                    )
                elif instrument_type == "equity_us":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_us
                    )
                elif instrument_type == "crypto":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.crypto
                    )
                elif instrument_type == "forex":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.forex
                    )
                elif instrument_type == "index":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.index
                    )
            except Exception as e:
                print(f"Instrument type 변환 에러: {e}")

        result = await db.execute(query)
        symbols = result.scalars().all()
        print(f"조회된 symbols: {symbols}")

        return {"symbols": sorted(symbols)}
    except Exception as e:
        print(f"Symbols API 에러: {e}")
        return {"symbols": []}


@router.get("/api/analysis/models")
async def get_unique_models(
    instrument_type: str | None = None,
    symbol: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    고유한 모델명 목록을 반환
    """
    print(f"Models API 호출됨 - instrument_type: {instrument_type}, symbol: {symbol}")

    try:
        query = select(PromptResult.model_name).distinct()

        if instrument_type and instrument_type.strip():
            print(f"모델명 필터링 적용: {instrument_type}")
            try:
                if instrument_type == "equity_kr":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_kr
                    )
                elif instrument_type == "equity_us":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_us
                    )
                elif instrument_type == "crypto":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.crypto
                    )
                elif instrument_type == "forex":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.forex
                    )
                elif instrument_type == "index":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.index
                    )
            except Exception as e:
                print(f"모델명 Instrument type 변환 에러: {e}")

        if symbol and symbol.strip():
            query = query.where(PromptResult.symbol == symbol)

        result = await db.execute(query)
        models = result.scalars().all()

        # None 값 제거하고 정렬
        models = [model for model in models if model]
        print(f"조회된 models: {models}")

        return {"models": sorted(models)}
    except Exception as e:
        print(f"Models API 에러: {e}")
        return {"models": []}


@router.get("/api/analysis/count")
async def get_analysis_count(
    symbol: str | None = None,
    instrument_type: str | None = None,
    model_name: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    분석 결과 총 개수를 반환
    """
    print(f"Count API 호출됨 - symbol: {symbol}, instrument_type: {instrument_type}")

    try:
        from sqlalchemy import func

        query = select(func.count(PromptResult.id))

        if symbol and symbol.strip():
            query = query.where(PromptResult.symbol == symbol)

        if model_name and model_name.strip():
            query = query.where(PromptResult.model_name == model_name)

        if instrument_type and instrument_type.strip():
            print(f"필터링 적용: {instrument_type}")
            try:
                if instrument_type == "equity_kr":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_kr
                    )
                elif instrument_type == "equity_us":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_us
                    )
                elif instrument_type == "crypto":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.crypto
                    )
                elif instrument_type == "forex":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.forex
                    )
                elif instrument_type == "index":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.index
                    )
            except Exception as e:
                print(f"Instrument type 변환 에러: {e}")

        result = await db.execute(query)
        count = result.scalar()
        print(f"조회된 count: {count}")

        return {"total_count": count or 0}
    except Exception as e:
        print(f"Count API 에러: {e}")
        return {"total_count": 0}


@router.get("/api/analysis")
async def get_analysis_results(
    symbol: str | None = None,
    instrument_type: str | None = None,
    model_name: str | None = None,
    page: int = 1,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """
    분석 결과를 조회하는 API
    """
    print(
        f"Analysis API 호출됨 - symbol: {symbol}, instrument_type: {instrument_type}, page: {page}, limit: {limit}"
    )

    try:
        query = select(PromptResult).order_by(desc(PromptResult.created_at))

        if symbol and symbol.strip():
            query = query.where(PromptResult.symbol == symbol)

        if model_name and model_name.strip():
            query = query.where(PromptResult.model_name == model_name)

        if instrument_type and instrument_type.strip():
            print(f"필터링 적용: {instrument_type}")
            try:
                if instrument_type == "equity_kr":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_kr
                    )
                elif instrument_type == "equity_us":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.equity_us
                    )
                elif instrument_type == "crypto":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.crypto
                    )
                elif instrument_type == "forex":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.forex
                    )
                elif instrument_type == "index":
                    query = query.where(
                        PromptResult.instrument_type == InstrumentType.index
                    )
            except Exception as e:
                print(f"Instrument type 변환 에러: {e}")

        # 페이지네이션
        offset = (page - 1) * limit
        query = query.offset(offset).limit(limit)

        result = await db.execute(query)
        records = result.scalars().all()
        print(f"조회된 records 수: {len(records)}")

        return [
            {
                "id": record.id,
                "symbol": record.symbol,
                "name": record.name,
                "instrument_type": record.instrument_type.value,
                "model_name": record.model_name,
                "prompt": record.prompt,
                "result": record.result,
                "created_at": record.created_at.isoformat()
                if record.created_at
                else None,
                "updated_at": record.updated_at.isoformat()
                if record.updated_at
                else None,
            }
            for record in records
        ]
    except Exception as e:
        print(f"Analysis API 에러: {e}")
        return []


@router.get("/api/analysis/{result_id}", response_model=dict)
async def get_analysis_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """
    특정 분석 결과를 조회하는 API
    """
    try:
        query = select(PromptResult).where(PromptResult.id == result_id)
        result = await db.execute(query)
        record = result.scalar_one_or_none()

        if not record:
            return {"error": "분석 결과를 찾을 수 없습니다."}

        return {
            "id": record.id,
            "symbol": record.symbol,
            "name": record.name,
            "instrument_type": record.instrument_type.value,
            "model_name": record.model_name,
            "prompt": record.prompt,
            "result": record.result,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }
    except Exception as e:
        print(f"Individual analysis API 에러: {e}")
        return {"error": f"데이터 조회 중 오류가 발생했습니다: {str(e)}"}
