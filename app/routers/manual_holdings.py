"""
Manual Holdings Router

수동 잔고 관리 API 엔드포인트
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.models.manual_holdings import BrokerType, MarketType
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.manual_holdings import (
    BrokerAccountCreate,
    BrokerAccountUpdate,
    BrokerAccountResponse,
    ManualHoldingCreate,
    ManualHoldingUpdate,
    ManualHoldingResponse,
    ManualHoldingBulkCreate,
    StockAliasCreate,
    StockAliasResponse,
    StockAliasSearchResult,
)
from app.services.broker_account_service import BrokerAccountService
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.stock_alias_service import StockAliasService, seed_toss_aliases

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/manual-holdings", tags=["Manual Holdings"])


# =============================================================================
# 웹 페이지
# =============================================================================

@router.get("/", response_class=HTMLResponse)
async def manual_holdings_page(request: Request):
    """수동 잔고 관리 페이지"""
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        "manual_holdings_dashboard.html",
        {
            "request": request,
            "user": user,
        }
    )


# =============================================================================
# 브로커 계좌 API
# =============================================================================

@router.get("/api/broker-accounts", response_model=list[BrokerAccountResponse])
async def list_broker_accounts(
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """브로커 계좌 목록 조회"""
    service = BrokerAccountService(db)
    accounts = await service.get_accounts(current_user.id)
    return accounts


@router.post("/api/broker-accounts", response_model=BrokerAccountResponse)
async def create_broker_account(
    data: BrokerAccountCreate,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """브로커 계좌 생성"""
    service = BrokerAccountService(db)

    # 중복 체크
    existing = await service.get_account_by_user_and_broker(
        current_user.id,
        data.broker_type,
        data.account_name,
        include_inactive=False,
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"이미 동일한 계좌가 존재합니다: {data.broker_type.value} - {data.account_name}"
        )

    account = await service.create_account(
        user_id=current_user.id,
        broker_type=data.broker_type,
        account_name=data.account_name,
        is_mock=data.is_mock,
    )
    return account


@router.put("/api/broker-accounts/{account_id}", response_model=BrokerAccountResponse)
async def update_broker_account(
    account_id: int,
    data: BrokerAccountUpdate,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """브로커 계좌 수정"""
    service = BrokerAccountService(db)
    account = await service.get_account_by_id(account_id, include_inactive=False)

    if not account or account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="계좌를 찾을 수 없습니다")

    updated = await service.update_account(
        account_id,
        **data.model_dump(exclude_unset=True)
    )
    return updated


@router.delete("/api/broker-accounts/{account_id}")
async def delete_broker_account(
    account_id: int,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """브로커 계좌 삭제"""
    service = BrokerAccountService(db)
    account = await service.get_account_by_id(account_id, include_inactive=False)

    if not account or account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="계좌를 찾을 수 없습니다")

    await service.delete_account(account_id)
    return {"success": True, "message": "계좌가 삭제되었습니다"}


# =============================================================================
# 수동 보유 종목 API
# =============================================================================

@router.get("/api/holdings", response_model=list[ManualHoldingResponse])
async def list_holdings(
    market_type: MarketType | None = None,
    broker_type: BrokerType | None = None,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """수동 보유 종목 목록 조회"""
    service = ManualHoldingsService(db)
    holdings = await service.get_holdings_by_user(
        current_user.id,
        market_type=market_type,
        broker_type=broker_type,
    )

    # 브로커 정보 추가
    result = []
    for h in holdings:
        data = ManualHoldingResponse.model_validate(h)
        data.broker_type = h.broker_account.broker_type
        data.account_name = h.broker_account.account_name
        result.append(data)

    return result


@router.post("/api/holdings", response_model=ManualHoldingResponse)
async def create_holding(
    data: ManualHoldingCreate,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """수동 보유 종목 등록"""
    # 브로커 계좌 조회 또는 생성
    account_service = BrokerAccountService(db)
    account = await account_service.get_or_create_default_account(
        current_user.id, data.broker_type
    )

    if data.account_name != "기본 계좌":
        account = await account_service.get_account_by_user_and_broker(
            current_user.id, data.broker_type, data.account_name
        )
        if not account:
            account = await account_service.create_account(
                current_user.id, data.broker_type, data.account_name
            )

    # 보유 종목 등록 (upsert)
    holdings_service = ManualHoldingsService(db)
    holding = await holdings_service.upsert_holding(
        broker_account_id=account.id,
        ticker=data.ticker,
        market_type=data.market_type,
        quantity=data.quantity,
        avg_price=data.avg_price,
        display_name=data.display_name,
    )

    result = ManualHoldingResponse.model_validate(holding)
    result.broker_type = account.broker_type
    result.account_name = account.account_name
    return result


@router.post("/api/holdings/bulk", response_model=list[ManualHoldingResponse])
async def create_holdings_bulk(
    data: ManualHoldingBulkCreate,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """수동 보유 종목 일괄 등록"""
    account_service = BrokerAccountService(db)
    account = await account_service.get_account_by_user_and_broker(
        current_user.id, data.broker_type, data.account_name
    )
    if not account:
        account = await account_service.create_account(
            current_user.id, data.broker_type, data.account_name
        )

    holdings_service = ManualHoldingsService(db)
    try:
        holdings = await holdings_service.bulk_create_holdings(
            account.id,
            [
                {
                    "ticker": h.ticker,
                    "market_type": h.market_type,
                    "quantity": h.quantity,
                    "avg_price": h.avg_price,
                    "display_name": h.display_name,
                }
                for h in data.holdings
            ]
        )
    except Exception as e:
        logger.exception("Bulk create failed")
        raise HTTPException(status_code=500, detail="일괄 등록 중 오류가 발생했습니다") from e

    result = []
    for h in holdings:
        r = ManualHoldingResponse.model_validate(h)
        r.broker_type = account.broker_type
        r.account_name = account.account_name
        result.append(r)

    return result


@router.put("/api/holdings/{holding_id}", response_model=ManualHoldingResponse)
async def update_holding(
    holding_id: int,
    data: ManualHoldingUpdate,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """수동 보유 종목 수정"""
    service = ManualHoldingsService(db)
    holding = await service.get_holding_by_id(holding_id)

    if not holding or holding.broker_account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="보유 종목을 찾을 수 없습니다")

    updated = await service.update_holding(
        holding_id,
        **data.model_dump(exclude_unset=True)
    )

    result = ManualHoldingResponse.model_validate(updated)
    result.broker_type = holding.broker_account.broker_type
    result.account_name = holding.broker_account.account_name
    return result


@router.delete("/api/holdings/{holding_id}")
async def delete_holding(
    holding_id: int,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """수동 보유 종목 삭제"""
    service = ManualHoldingsService(db)
    holding = await service.get_holding_by_id(holding_id)

    if not holding or holding.broker_account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="보유 종목을 찾을 수 없습니다")

    await service.delete_holding(holding_id)
    return {"success": True, "message": "보유 종목이 삭제되었습니다"}


# =============================================================================
# 종목 별칭 API
# =============================================================================

@router.get("/api/stock-aliases/search", response_model=StockAliasSearchResult)
async def search_stock_aliases(
    q: str = Query(..., min_length=1, description="검색어"),
    market_type: MarketType | None = None,
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """종목 별칭 검색"""
    service = StockAliasService(db)
    aliases = await service.search_by_alias(q, market_type, limit)
    return StockAliasSearchResult(
        aliases=[StockAliasResponse.model_validate(a) for a in aliases],
        total=len(aliases),
    )


@router.post("/api/stock-aliases", response_model=StockAliasResponse)
async def create_stock_alias(
    data: StockAliasCreate,
    db: AsyncSession = Depends(get_db),
):
    """종목 별칭 등록"""
    service = StockAliasService(db)

    # 중복 체크
    existing = await service.get_ticker_by_alias(data.alias, data.market_type)
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"이미 등록된 별칭입니다: {data.alias}"
        )

    alias = await service.create_alias(
        ticker=data.ticker,
        market_type=data.market_type,
        alias=data.alias,
        source=data.source,
    )
    return StockAliasResponse.model_validate(alias)


@router.post("/api/stock-aliases/seed-toss")
async def seed_toss_stock_aliases(
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """토스 종목 별칭 기본 데이터 시딩"""
    count = await seed_toss_aliases(db)
    return {"success": True, "created": count}
