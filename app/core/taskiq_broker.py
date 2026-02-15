import logging

from taskiq import TaskiqMiddleware
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.core.config import settings
from app.monitoring.sentry import init_sentry
from app.monitoring.trade_notifier import get_trade_notifier

logger = logging.getLogger(__name__)


class WorkerInitMiddleware(TaskiqMiddleware):
    async def startup(self) -> None:
        if not self.broker.is_worker_process:
            return

        init_sentry(
            service_name="auto-trader-worker",
            enable_sqlalchemy=True,
            enable_httpx=True,
        )

        if settings.telegram_token and settings.telegram_chat_id:
            try:
                trade_notifier = get_trade_notifier()
                trade_notifier.configure(
                    bot_token=settings.telegram_token,
                    chat_ids=settings.telegram_chat_ids,
                    enabled=True,
                )
                logger.info("Worker: Trade notifier initialized")
            except Exception as exc:
                logger.error(
                    "Worker: Failed to initialize trade notifier: %s",
                    exc,
                    exc_info=True,
                )


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
