from app.tasks import (
    crypto_pending_order_alert_tasks,
    daily_scan_tasks,
    intraday_order_review_tasks,
    kr_candles_tasks,
    kr_symbol_universe_tasks,
    research_run_refresh_tasks,
    upbit_symbol_universe_tasks,
    us_candles_tasks,
    us_symbol_universe_tasks,
    watch_proximity_tasks,
    watch_scan_tasks,
)

TASKIQ_TASK_MODULES = (
    crypto_pending_order_alert_tasks,
    daily_scan_tasks,
    intraday_order_review_tasks,
    research_run_refresh_tasks,
    watch_proximity_tasks,
    watch_scan_tasks,
    kr_candles_tasks,
    kr_symbol_universe_tasks,
    upbit_symbol_universe_tasks,
    us_candles_tasks,
    us_symbol_universe_tasks,
)
