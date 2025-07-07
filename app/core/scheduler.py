from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.jobs.screener import screen_once_async
from app.core.config import settings
import pytz

sched = AsyncIOScheduler(timezone=pytz.timezone("Asia/Seoul"))

def start_scheduler():
    # “0 * * * *” → cron 표현 → 파싱
    minute, hour, *_ = settings.cron.split()
    sched.add_job(screen_once_async, "cron", minute=minute, hour=hour)
    sched.start()