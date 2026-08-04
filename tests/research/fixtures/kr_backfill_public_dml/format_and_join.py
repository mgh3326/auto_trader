PARTS = ["public", "kr_candles_1m"]
TARGET = ".".join(PARTS)
SQL = "UPDATE {table} SET close = 1".format(table=TARGET)  # noqa: UP032 - regression form
