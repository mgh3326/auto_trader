"""Documentation may mention public.kr_candles_1m without executing SQL."""

SCHEMA = "research"
TABLE = "kr_candles_1m"
SQL = f"INSERT INTO {SCHEMA}.{TABLE} (symbol) VALUES ('005930')"
NOTE = "public schema is prohibited for this backfill"
