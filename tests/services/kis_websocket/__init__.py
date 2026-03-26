def build_domestic_message(
    *,
    symbol: str = "012450",
    side: str = "02",
    filled_qty: str = "2",
    filled_price: str = "1135000",
    ord_tmd: str = "093001",
    fill_yn: str = "2",
    order_id: str = "0030145286",
) -> str:
    """
    Build an official H0STCNI0 (Domestic) message.
    """
    # DOMESTIC_OFFICIAL_FILL_FIELDS = {
    #     "order_id": 2,
    #     "side": 4,
    #     "symbol": 8,
    #     "filled_qty": 9,
    #     "filled_price": 10,
    #     "filled_at": 11,
    #     "fill_yn": 13,
    # }
    payload = (
        f"mgh3326^6762259301^{order_id}^0000000000^{side}^0^00^00^{symbol}^"
        f"{filled_qty}^{filled_price}^{ord_tmd}^N^{fill_yn}^Y^0000^2^홍길동^0^KRX^N^^00^00000000^한화에어로^{filled_price}"
    )
    return f"0|H0STCNI0|1|{payload}"


def build_official_h0gscni0_message(
    *,
    side: str = "02",
    rctf_cls: str = "0",
    ord_tmd: str = "153045",
    symbol: str = "AAPL",
    filled_qty: str = "10",
    filled_price: str = "248.50",
    order_qty: str = "0000000010",
    cntg_yn: str = "2",
    rfus_yn: str = "0",
    acpt_yn: str = "1",
    reject_reason: str = "",
    trailing_field: str = "NASDAQ",
) -> str:
    """
    Build an official H0GSCNI0 (Overseas) message using the documented field order.
    """
    # OVERSEAS_FILL_FIELDS = {
    #     "side": 4,
    #     "rctf_cls": 5,
    #     "filled_at": 6,
    #     "symbol": 7,
    #     "filled_qty": 8,
    #     "filled_price": 9,
    #     "order_qty": 10,
    #     "cntg_yn": 11,
    #     "fill_yn": 11,
    #     "rfus_yn": 12,
    #     "acpt_yn": 13,
    # }
    payload = (
        f"12345678^01^ORD000001^0000000000^{side}^{rctf_cls}^{ord_tmd}^{symbol}^"
        f"{filled_qty}^{filled_price}^{order_qty}^{cntg_yn}^{rfus_yn}^{acpt_yn}^{reject_reason}^{trailing_field}"
    )
    return f"0|H0GSCNI0|1|{payload}"


def build_overseas_message(**kwargs) -> str:
    """DEPRECATED: Use build_official_h0gscni0_message instead."""
    return build_official_h0gscni0_message(**kwargs)
