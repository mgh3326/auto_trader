import logging

from taskiq import TaskiqMiddleware
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.core.config import settings
from app.monitoring.sentry import init_sentry
from app.monitoring.trade_notifier import get_trade_notifier

logger = logging.getLogger(__name__)


class WorkerInitMiddleware(TaskiqMiddleware):
    async def startup(self) -> None:
        if self.broker.is_worker_process:
            init_sentry(
                service_name="auto-trader-worker",
                enable_sqlalchemy=True,
                enable_httpx=True,
            )

            # Check if any notification system is configured
            has_discord = any([
                settings.discord_webhook_us,
                settings.discord_webhook_kr,
                settings.discord_webhook_crypto,
                settings.discord_webhook_alerts,
            ])
            has_telegram = settings.telegram_token and settings.telegram_chat_id

            if has_discord or has_telegram:
                try:
                    trade_notifier = get_trade_notifier()
                    bot_token = settings.telegram_token or ""
                    chat_ids = settings.telegram_chat_ids if has_telegram else []

                    trade_notifier.configure(
                        bot_token=bot_token,
                        chat_ids=chat_ids,
                        enabled=True,
                        discord_webhook_us=settings.discord_webhook_us,
                        discord_webhook_kr=settings.discord_webhook_kr,
                        discord_webhook_crypto=settings.discord_webhook_crypto,
                        discord_webhook_alerts=settings.discord_webhook_alerts,
                    )
                    logger.info("Worker: Trade notifier initialized")
                except Exception as exc:
                    logger.error(
                        "Worker: Failed to initialize trade notifier: %s",
                        exc,
                        exc_info=True,
                    )
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
