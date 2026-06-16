import logging

from taskiq import TaskiqMiddleware
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.core.config import settings
from app.monitoring.sentry import init_sentry
from app.monitoring.trade_notifier.runtime import configure_trade_notifier_from_settings

logger = logging.getLogger(__name__)


class WorkerInitMiddleware(TaskiqMiddleware):
    async def startup(self) -> None:
        if self.broker.is_worker_process:
            init_sentry(
                service_name="auto-trader-worker",
                enable_sqlalchemy=True,
                enable_httpx=True,
            )

            configure_trade_notifier_from_settings(log_context="Worker trade notifier")
            return

        if getattr(self.broker, "is_scheduler_process", False):
            init_sentry(service_name="auto-trader-scheduler")


result_backend = RedisAsyncResultBackend(
    redis_url=settings.get_redis_url(),
    result_ex_time=3600,
)

broker = (
    ListQueueBroker(
        url=settings.get_redis_url(),
        queue_name="auto-trader",
    )
    .with_result_backend(result_backend)
    .with_middlewares(WorkerInitMiddleware())
)
