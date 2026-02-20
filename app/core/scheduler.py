import logging
import uuid

import redis.asyncio as redis
from taskiq import TaskiqScheduler
from taskiq.abc.schedule_source import ScheduleSource
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.scheduler.scheduled_task import ScheduledTask

from app.core.config import settings
from app.core.taskiq_broker import broker

logger = logging.getLogger(__name__)


class RedisLeaderScheduleSource(ScheduleSource):
    def __init__(
        self,
        source: ScheduleSource,
        redis_url: str,
        lock_key: str = "auto-trader:scheduler:leader",
        lock_ttl_seconds: int = 90,
    ) -> None:
        self._source = source
        self._redis_url = redis_url
        self._lock_key = lock_key
        self._lock_ttl_seconds = lock_ttl_seconds
        self._instance_id = uuid.uuid4().hex
        self._redis_client: redis.Redis | None = None
        self._owns_lock = False

    async def startup(self) -> None:
        await self._source.startup()
        self._redis_client = redis.from_url(self._redis_url, decode_responses=True)

    async def shutdown(self) -> None:
        if self._redis_client is not None:
            if self._owns_lock:
                await self._release_lock()
            await self._redis_client.aclose()
            self._redis_client = None
        await self._source.shutdown()

    async def get_schedules(self) -> list[ScheduledTask]:
        has_lock = await self._acquire_or_refresh_lock()
        if not has_lock:
            self._owns_lock = False
            return []

        self._owns_lock = True
        return await self._source.get_schedules()

    def post_send(self, task: ScheduledTask):
        return self._source.post_send(task)

    async def add_schedule(self, schedule: ScheduledTask) -> None:
        await self._source.add_schedule(schedule)

    async def delete_schedule(self, schedule_id: str) -> None:
        await self._source.delete_schedule(schedule_id)

    async def _acquire_or_refresh_lock(self) -> bool:
        if self._redis_client is None:
            logger.error("Scheduler lock source used before startup")
            return False

        try:
            owner = await self._redis_client.get(self._lock_key)
            if owner == self._instance_id:
                await self._redis_client.expire(self._lock_key, self._lock_ttl_seconds)
                return True

            if owner is None:
                acquired = await self._redis_client.set(
                    self._lock_key,
                    self._instance_id,
                    nx=True,
                    ex=self._lock_ttl_seconds,
                )
                return bool(acquired)

            return False
        except Exception:
            logger.exception("Failed to acquire scheduler leader lock")
            return False

    async def _release_lock(self) -> None:
        if self._redis_client is None:
            return

        try:
            await self._redis_client.eval(
                (
                    "if redis.call('get', KEYS[1]) == ARGV[1] "
                    "then return redis.call('del', KEYS[1]) "
                    "else return 0 end"
                ),
                1,
                self._lock_key,
                self._instance_id,
            )
        except Exception:
            logger.exception("Failed to release scheduler leader lock")


label_source = LabelScheduleSource(broker)
leader_source = RedisLeaderScheduleSource(
    source=label_source,
    redis_url=settings.get_redis_url(),
)

sched = TaskiqScheduler(
    broker=broker,
    sources=[leader_source],
)


def start_scheduler() -> TaskiqScheduler:
    return sched
