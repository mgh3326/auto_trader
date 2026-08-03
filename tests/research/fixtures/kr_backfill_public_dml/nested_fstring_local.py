def make_sql() -> str:
    schema = "public"
    table = "kr_candles_1m"
    target = f"{schema}.{table}"
    return f"INSERT INTO {target} (symbol) VALUES ('005930')"
