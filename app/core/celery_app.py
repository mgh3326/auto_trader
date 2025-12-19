from celery import Celery

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


@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    """Setup periodic tasks."""
    # Example: sender.add_periodic_task(300.0, test.s(), name='add every 10')
    pass


from celery.signals import task_failure, worker_process_init


@task_failure.connect
def handle_task_failure(
    sender=None,
    task_id=None,
    exception=None,
    args=None,
    kwargs=None,
    traceback=None,
    einfo=None,
    **kw,
):
    """Celery 태스크 실패 시 Telegram 알림 전송."""
    import asyncio
    import logging

    from app.monitoring.error_reporter import get_error_reporter

    logger = logging.getLogger(__name__)

    try:
        error_reporter = get_error_reporter()
        if error_reporter._enabled:
            # 추가 컨텍스트 정보
            additional_context = {
                "task_name": sender.name if sender else "unknown",
                "task_id": task_id or "unknown",
                "args": str(args)[:200] if args else "None",
                "kwargs": str(kwargs)[:200] if kwargs else "None",
            }

            # 동기 환경에서 비동기 함수 호출
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    error_reporter.send_error_to_telegram(
                        error=exception,
                        additional_context=additional_context,
                    )
                )
            finally:
                loop.close()

            logger.info(
                f"Task failure reported to Telegram: {sender.name if sender else 'unknown'}"
            )
    except Exception as e:
        logger.error(f"Failed to report task failure to Telegram: {e}")


@worker_process_init.connect
def init_worker(**kwargs):
    """Initialize monitoring when worker process starts."""
    import logging

    from redis import Redis

    from app.core.config import settings
    from app.monitoring.error_reporter import get_error_reporter
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

    # Initialize Error Reporter
    if settings.ERROR_REPORTING_ENABLED:
        try:
            redis_client = Redis.from_url(
                settings.get_redis_url(),
                decode_responses=True,
                max_connections=settings.redis_max_connections,
            )

            error_reporter = get_error_reporter()
            error_reporter.configure(
                bot_token=settings.telegram_token,
                chat_id=settings.ERROR_REPORTING_CHAT_ID
                or (
                    settings.telegram_chat_ids[0] if settings.telegram_chat_ids else ""
                ),
                redis_client=redis_client,
                enabled=True,
                duplicate_window=settings.ERROR_DUPLICATE_WINDOW,
            )
            logger.info("Worker: Error reporter initialized")
        except Exception as e:
            logger.error(f"Worker: Failed to initialize error reporter: {e}")
