DOMESTIC_EXECUTION_TR_REAL = "H0STCNI0"
OVERSEAS_EXECUTION_TR_REAL = "H0GSCNI0"
DOMESTIC_EXECUTION_TR_MOCK = "H0STCNI9"
OVERSEAS_EXECUTION_TR_MOCK = "H0GSCNI9"

DOMESTIC_EXECUTION_TR = DOMESTIC_EXECUTION_TR_REAL
OVERSEAS_EXECUTION_TR = OVERSEAS_EXECUTION_TR_REAL

DOMESTIC_EXECUTION_TR_CODES = {
    DOMESTIC_EXECUTION_TR_REAL,
    DOMESTIC_EXECUTION_TR_MOCK,
}
OVERSEAS_EXECUTION_TR_CODES = {
    OVERSEAS_EXECUTION_TR_REAL,
    OVERSEAS_EXECUTION_TR_MOCK,
}
EXECUTION_TR_CODES = DOMESTIC_EXECUTION_TR_CODES | OVERSEAS_EXECUTION_TR_CODES

_SIDE_MAP = {
    "01": "ask",
    "1": "ask",
    "S": "ask",
    "SELL": "ask",
    "ASK": "ask",
    "매도": "ask",
    "02": "bid",
    "2": "bid",
    "B": "bid",
    "BUY": "bid",
    "BID": "bid",
    "매수": "bid",
}

_US_SYMBOL_RESERVED_TOKENS = {
    "PROD",
    "RESERVED",
    "ENV",
    "HTS",
    "NASD",
    "NASDAQ",
    "NYSE",
    "AMEX",
    "KRX",
}

OVERSEAS_FILL_FIELDS = {
    "side": 4,
    "rctf_cls": 5,
    "filled_at": 6,
    "symbol": 7,
    "filled_qty": 8,
    "filled_price": 9,
    "order_qty": 10,
    "cntg_yn": 11,
    "fill_yn": 11,
    "rfus_yn": 12,
    "acpt_yn": 13,
}

OVERSEAS_SIDE_MAP = {
    "01": "ask",
    "1": "ask",
    "S": "ask",
    "02": "bid",
    "2": "bid",
    "B": "bid",
}

DOMESTIC_OFFICIAL_FILL_FIELDS = {
    "order_id": 2,
    "side": 4,
    "symbol": 8,
    "filled_qty": 9,
    "filled_price": 10,
    "filled_at": 11,
    "fill_yn": 13,
}

DOMESTIC_COMPACT_FILL_FIELDS = {
    "symbol": 0,
    "side": 1,
    "order_id": 2,
    "first_numeric": 3,
    "second_numeric": 4,
    "filled_at": 5,
}


class KISSubscriptionAckError(RuntimeError):
    """Structured ACK failure for KIS subscription."""

    def __init__(self, tr_id: str, rt_cd: str, msg_cd: str, msg1: str):
        self.tr_id = tr_id
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd
        self.msg1 = msg1
        super().__init__(
            f"Subscription failed: tr_id={tr_id} rt_cd={rt_cd} "
            f"msg_cd={msg_cd} msg1={msg1}"
        )
