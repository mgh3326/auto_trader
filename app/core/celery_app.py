from celery import Celery

from app.core.config import settings


def _get_broker_url() -> str:
    # Use the unified Redis URL for both broker and backend for simplicity
    return settings.get_redis_url()


celery_app = Celery(
    "auto_trader",
    broker=_get_broker_url(),
    backend=_get_broker_url(),
    include=["app.tasks.analyze"],
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


