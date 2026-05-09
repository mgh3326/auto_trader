"""Shared FastAPI Depends helpers for limit/offset and page/page_size pagination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Query


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """Parsed limit + offset query parameters."""

    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class PageParams:
    """Parsed page + page_size query parameters."""

    page: int
    page_size: int


def pagination_params(
    *,
    default_limit: int = 50,
    max_limit: int = 200,
) -> Callable[..., PaginationParams]:
    """Return a FastAPI Depends callable for limit/offset pagination.

    Usage::

        dep = pagination_params(default_limit=20, max_limit=100)

        @router.get("/items")
        async def list_items(p: PaginationParams = dep, ...):
            ...
    """

    def _dep(
        limit: int | None = Query(
            default=None,
            ge=1,
            le=max_limit,
            description=f"페이지 크기 (최대 {max_limit})",
        ),
        offset: int | None = Query(
            default=None,
            ge=0,
            description="건너뛸 항목 수",
        ),
    ) -> PaginationParams:
        return PaginationParams(
            limit=min(limit if limit is not None else default_limit, max_limit),
            offset=offset if offset is not None else 0,
        )

    return _dep


def page_params(
    *,
    default_size: int = 50,
    max_size: int = 200,
) -> Callable[..., PageParams]:
    """Return a FastAPI Depends callable for page/page_size pagination.

    Usage::

        dep = page_params(default_size=50, max_size=200)

        @router.get("/stocks")
        async def list_stocks(p: PageParams = dep, ...):
            ...
    """

    def _dep(
        page: int | None = Query(
            default=None,
            ge=1,
            description="페이지 번호 (1부터)",
        ),
        page_size: int | None = Query(
            default=None,
            ge=1,
            le=max_size,
            description=f"페이지 크기 (최대 {max_size})",
        ),
    ) -> PageParams:
        return PageParams(
            page=page if page is not None else 1,
            page_size=min(
                page_size if page_size is not None else default_size, max_size
            ),
        )

    return _dep
