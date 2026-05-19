"""ROB-273 — recursive JSON normalisation helper."""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal

import pytest

from app.services.action_report.common.jsonable import to_jsonable


class _Side(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


def test_passes_through_primitives():
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable(1) == 1
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable("hello") == "hello"


def test_decimal_becomes_string():
    assert to_jsonable(Decimal("1.23")) == "1.23"
    # precision-sensitive — bare float would truncate the trailing zero
    assert to_jsonable(Decimal("0.10")) == "0.10"


def test_datetime_becomes_iso8601():
    moment = dt.datetime(2026, 5, 19, 12, 0, tzinfo=dt.UTC)
    assert to_jsonable(moment) == "2026-05-19T12:00:00+00:00"


def test_date_and_time_become_iso():
    assert to_jsonable(dt.date(2026, 5, 19)) == "2026-05-19"
    assert to_jsonable(dt.time(12, 30)) == "12:30:00"


def test_uuid_becomes_string():
    u = uuid.UUID("4b8a5e4e-1234-5678-9abc-def012345678")
    assert to_jsonable(u) == str(u)


def test_enum_becomes_value():
    assert to_jsonable(_Side.BUY) == "buy"


def test_nested_recursive_normalisation():
    payload = {
        "threshold": Decimal("100.5"),
        "as_of": dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        "items": [
            {
                "id": uuid.UUID("4b8a5e4e-1234-5678-9abc-def012345678"),
                "price": Decimal("1.1"),
            },
            {
                "id": uuid.UUID("aaaaaaaa-1234-5678-9abc-def012345678"),
                "price": Decimal("2.2"),
            },
        ],
        "set_field": {Decimal("1"), Decimal("2")},
    }
    out = to_jsonable(payload)
    assert out["threshold"] == "100.5"
    assert out["as_of"] == "2026-05-19T00:00:00+00:00"
    assert out["items"][0]["price"] == "1.1"
    assert out["items"][0]["id"] == "4b8a5e4e-1234-5678-9abc-def012345678"
    # set order is unstable — compare as a set after re-normalisation
    assert set(out["set_field"]) == {"1", "2"}


def test_dict_keys_coerced_to_string():
    out = to_jsonable({1: "a", "b": 2})
    assert out == {"1": "a", "b": 2}


def test_tuple_becomes_list():
    assert to_jsonable((1, 2, Decimal("3"))) == [1, 2, "3"]


def test_pydantic_models_round_trip():
    from app.schemas.investment_reports import WatchConditionPayload

    cond = WatchConditionPayload(
        metric="price",
        operator="above",
        threshold=Decimal("12345.67"),
    )
    out = to_jsonable(cond)
    assert out["metric"] == "price"
    assert out["operator"] == "above"
    # threshold serialises via Pydantic JSON mode → str.
    assert isinstance(out["threshold"], str)
    assert out["threshold"] == "12345.67"


def test_bytes_rejected_explicitly():
    with pytest.raises(TypeError):
        to_jsonable(b"deadbeef")


def test_unknown_type_rejected():
    class _Opaque:
        pass

    with pytest.raises(TypeError):
        to_jsonable(_Opaque())
