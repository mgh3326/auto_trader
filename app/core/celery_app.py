from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings


def _get_broker_url() -> str:
    # Use the unified Redis URL for both broker and backend for simplicity
    return settings.get_redis_url()


celery_app = Celery(
    "auto_trader",
    broker=_get_broker_url(),
    backend=_get_broker_url(),
    include=["app.tasks.analyze", "app.tasks.kis"],
)

# Reasonable defaults
celery_app.conf.update(
    task_ignore_result=False,
    result_expires=3600,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Seoul",
    enable_utc=False,
)


@celery_app.on_after_configure.connect  # type: ignore[union-attr]
def setup_periodic_tasks(sender, **kwargs):
    """Setup periodic tasks."""
    # Example: sender.add_periodic_task(300.0, test.s(), name='add every 10')
    pass


@worker_process_init.connect
def init_worker(**kwargs):
    """Initialize monitoring when worker process starts."""
    import logging

    from app.core.config import settings
    from app.monitoring.trade_notifier import get_trade_notifier

    logger = logging.getLogger(__name__)

    # Initialize Trade Notifier
    if settings.telegram_token and settings.telegram_chat_id:
        try:
            trade_notifier = get_trade_notifier()
            trade_notifier.configure(
                bot_token=settings.telegram_token,
                chat_ids=settings.telegram_chat_ids,
                enabled=True,
            )
            logger.info("Worker: Trade notifier initialized")
        except Exception as e:
            logger.error(f"Worker: Failed to initialize trade notifier: {e}")
