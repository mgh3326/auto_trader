"""Unit tests for app/schemas/pagination — PaginationMeta and ListResponse."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# PaginationMeta
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pagination_meta_valid():
    from app.schemas.pagination import PaginationMeta

    meta = PaginationMeta(total=100, limit=10, offset=0, has_more=True)

    assert meta.total == 100
    assert meta.limit == 10
    assert meta.offset == 0
    assert meta.has_more is True


@pytest.mark.unit
def test_pagination_meta_has_more_false():
    from app.schemas.pagination import PaginationMeta

    meta = PaginationMeta(total=5, limit=10, offset=0, has_more=False)

    assert meta.has_more is False


@pytest.mark.unit
def test_pagination_meta_serializes_to_dict():
    from app.schemas.pagination import PaginationMeta

    meta = PaginationMeta(total=50, limit=20, offset=20, has_more=True)
    d = meta.model_dump()

    assert d == {"total": 50, "limit": 20, "offset": 20, "has_more": True}


@pytest.mark.unit
def test_pagination_meta_deserializes_from_dict():
    from app.schemas.pagination import PaginationMeta

    meta = PaginationMeta.model_validate(
        {"total": 3, "limit": 10, "offset": 0, "has_more": False}
    )

    assert meta.total == 3


@pytest.mark.unit
def test_pagination_meta_rejects_negative_total():
    from app.schemas.pagination import PaginationMeta

    with pytest.raises(ValidationError):
        PaginationMeta(total=-1, limit=10, offset=0, has_more=False)


@pytest.mark.unit
def test_pagination_meta_rejects_negative_limit():
    from app.schemas.pagination import PaginationMeta

    with pytest.raises(ValidationError):
        PaginationMeta(total=10, limit=0, offset=0, has_more=False)


@pytest.mark.unit
def test_pagination_meta_rejects_negative_offset():
    from app.schemas.pagination import PaginationMeta

    with pytest.raises(ValidationError):
        PaginationMeta(total=10, limit=10, offset=-1, has_more=False)


# ---------------------------------------------------------------------------
# ListResponse[T]
# ---------------------------------------------------------------------------


class _Item(BaseModel):
    id: int
    name: str


@pytest.mark.unit
def test_list_response_instantiation():
    from app.schemas.pagination import ListResponse, PaginationMeta

    meta = PaginationMeta(total=2, limit=10, offset=0, has_more=False)
    response = ListResponse[_Item](
        items=[_Item(id=1, name="a"), _Item(id=2, name="b")],
        pagination=meta,
    )

    assert len(response.items) == 2
    assert response.items[0].id == 1
    assert response.pagination.total == 2


@pytest.mark.unit
def test_list_response_empty_items():
    from app.schemas.pagination import ListResponse, PaginationMeta

    meta = PaginationMeta(total=0, limit=10, offset=0, has_more=False)
    response = ListResponse[_Item](items=[], pagination=meta)

    assert response.items == []


@pytest.mark.unit
def test_list_response_serializes_to_dict():
    from app.schemas.pagination import ListResponse, PaginationMeta

    meta = PaginationMeta(total=1, limit=10, offset=0, has_more=False)
    response = ListResponse[_Item](
        items=[_Item(id=99, name="z")],
        pagination=meta,
    )
    d = response.model_dump()

    assert d["items"] == [{"id": 99, "name": "z"}]
    assert d["pagination"]["total"] == 1


@pytest.mark.unit
def test_list_response_json_schema_generated():
    """Pydantic v2 must be able to generate JSON schema for a concrete generic."""
    from app.schemas.pagination import ListResponse

    schema = ListResponse[_Item].model_json_schema()

    assert "properties" in schema
    assert "items" in schema["properties"]
    assert "pagination" in schema["properties"]


@pytest.mark.unit
def test_list_response_rejects_non_list_items():
    from app.schemas.pagination import ListResponse, PaginationMeta

    meta = PaginationMeta(total=1, limit=10, offset=0, has_more=False)

    with pytest.raises((ValidationError, TypeError)):
        ListResponse[_Item](items="not-a-list", pagination=meta)  # type: ignore[arg-type]
