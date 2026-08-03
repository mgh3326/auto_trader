"""Example only: INSERT INTO public.kr_candles_1m (symbol) VALUES ('005930')."""

SCHEMA = "research"
TABLE = "kr_candles_1m"
SQL = f"INSERT INTO {SCHEMA}.{TABLE} (symbol) VALUES ('005930')"
NOTE = "public schema is prohibited for this backfill"
TEMP_SQL = "CREATE TEMP TABLE session_scratch (id integer)"
