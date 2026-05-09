"""Shared pagination primitives.

Usage (라우터에서):
    from app.schemas.pagination import ListResponse, PaginationMeta

    meta = PaginationMeta(
        total=total_count,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total_count,
    )
    return ListResponse[MyItemSchema](items=rows, pagination=meta)

Note:
    has_more 계산은 호출 쪽(라우터/서비스)의 책임이다.
    이 모듈은 계산 로직 없이 단순 선언만 제공한다.

후속:
    W2-2 에서 기존 라우터/스키마를 ListResponse 기반으로 마이그레이션.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PaginationMeta(BaseModel):
    """페이지네이션 메타데이터."""

    total: int = Field(..., ge=0, description="전체 항목 수")
    limit: int = Field(..., ge=1, description="페이지당 최대 항목 수")
    offset: int = Field(..., ge=0, description="조회 시작 오프셋")
    has_more: bool = Field(..., description="다음 페이지 존재 여부")


class ListResponse[T](BaseModel):
    """제네릭 목록 응답 래퍼.

    Example::

        ListResponse[UserSchema](items=users, pagination=meta)
    """

    items: list[T]
    pagination: PaginationMeta
