from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.screener import screen_once_async


@broker.task(
    task_name="scheduler.screen_once_async",
    schedule=[
        {
            "cron": settings.cron,
            "cron_offset": "Asia/Seoul",
        }
    ],
)
async def screen_once_task() -> None:
    await screen_once_async()
