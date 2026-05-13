from __future__ import annotations

from decimal import Decimal

from app.services.naver_stock.parser import (
    parse_domestic_stock_default,
    parse_theme_stocklist,
    parse_upjong_theme_list,
    sanitize_raw_payload,
)


def _stock_payload(order_type: str) -> dict:
    return {
        "result": {
            "stocks": [
                {
                    "itemcode": "5930",
                    "itemname": f"삼성전자 {order_type}",
                    "rank": "1",
                    "nowPrice": "78,500",
                    "prevChange": "1,200",
                    "prevChangeRate": "1.55%",
                    "tradeVolume": "14,500,000",
                    "tradeAmount": "1138250000000",
                    "marketSum": "468630000000000",
                    "marketAlertType": "NONE",
                    "headers": {"cookie": "drop-me"},
                    "discussion": "drop community text",
                }
            ]
        }
    }


def test_parse_domestic_stock_default_handles_naver_revamped_keys_for_core_order_types():
    for order_type in ("up", "quantTop", "priceTop", "searchTop"):
        parsed = parse_domestic_stock_default(_stock_payload(order_type))

        assert parsed.warnings == ()
        assert len(parsed.rows) == 1
        row = parsed.rows[0]
        assert row.symbol == "005930"
        assert row.name == f"삼성전자 {order_type}"
        assert row.rank == 1
        assert row.price == Decimal("78500")
        assert row.change_amount == Decimal("1200")
        assert row.change_rate == Decimal("1.55")
        assert row.volume == 14_500_000
        assert row.trade_value == Decimal("1138250000000")
        assert row.market_cap == Decimal("468630000000000")
        assert "headers" not in row.raw_payload
        assert "discussion" not in row.raw_payload


def test_parse_theme_and_upjong_lists_and_sanitize_raw_payload():
    payload = {
        "data": [
            {
                "themeNo": "591",
                "themeName": "반도체",
                "rank": 1,
                "changeRate": "3.21",
                "totalMarketSum": "1000000000",
                "stockCount": 12,
                "leaderSymbols": [
                    {"itemcode": "000660", "itemname": "SK하이닉스", "userId": "drop"}
                ],
                "html": "<html>drop</html>",
                "comment": "drop ugc",
            }
        ]
    }

    parsed = parse_upjong_theme_list(payload, event_kind="theme")

    assert parsed.warnings == ()
    row = parsed.rows[0]
    assert row.source_key == "591"
    assert row.naver_theme_no == "591"
    assert row.name == "반도체"
    assert row.change_rate == Decimal("3.21")
    assert row.leader_symbols == ({"symbol": "000660", "name": "SK하이닉스"},)
    assert "html" not in row.raw_payload
    assert "comment" not in row.raw_payload

    upjong = parse_upjong_theme_list(
        {"list": [{"upjongCode": "G101", "upjongName": "전기전자"}]},
        event_kind="upjong",
    )
    assert upjong.rows[0].source_key == "G101"
    assert upjong.rows[0].naver_upjong_code == "G101"

    live_shape_upjong = parse_upjong_theme_list(
        {
            "list": [
                {
                    "no": "327",
                    "type": "upjong",
                    "name": "디스플레이패널",
                    "changeRate": "10.25",
                    "totalAccAmount": "526025179",
                    "totalMarketSum": "7634625",
                    "leadingItem": "2,034220,LG디스플레이|2,191410,육일씨엔에쓰",
                }
            ]
        },
        event_kind="upjong",
    )
    assert live_shape_upjong.warnings == ()
    assert live_shape_upjong.rows[0].source_key == "327"
    assert live_shape_upjong.rows[0].name == "디스플레이패널"
    assert live_shape_upjong.rows[0].leader_symbols == (
        {"symbol": "034220", "name": "LG디스플레이"},
        {"symbol": "191410", "name": "육일씨엔에쓰"},
    )


def test_parse_theme_stocklist_reuses_stock_parser_and_sanitizer():
    parsed = parse_theme_stocklist(
        {
            "itemList": [
                {"itemCode": "035420", "itemName": "NAVER", "nowPrice": "200000"}
            ]
        }
    )
    assert parsed.rows[0].symbol == "035420"
    assert parsed.rows[0].price == Decimal("200000")


def test_sanitize_raw_payload_drops_tracking_and_user_generated_fields():
    sanitized = sanitize_raw_payload(
        {
            "safe": "value",
            "Cookie": "secret",
            "headers": {"Authorization": "secret"},
            "authorId": "user-1",
            "userNickname": "nick",
            "discussion": "community body",
            "html": "<body>body</body>",
            "nested": {"trackingId": "abc", "price": 1},
        }
    )
    assert sanitized == {"safe": "value", "nested": {"price": 1}}
