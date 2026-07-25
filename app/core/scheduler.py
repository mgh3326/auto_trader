from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.core.taskiq_broker import broker, retry_schedule_source

sched = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker), retry_schedule_source],
)


def start_scheduler() -> TaskiqScheduler:
    return sched
