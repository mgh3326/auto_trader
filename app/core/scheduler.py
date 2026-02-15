from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.core.taskiq_broker import broker

sched = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)


def start_scheduler() -> TaskiqScheduler:
    return sched
