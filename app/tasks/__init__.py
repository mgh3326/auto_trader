from app.tasks import daily_scan_tasks, kr_symbol_universe_tasks, watch_scan_tasks

TASKIQ_TASK_MODULES = (daily_scan_tasks, watch_scan_tasks, kr_symbol_universe_tasks)
