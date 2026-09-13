"""Default-off, scheduleless fill/watch context-only consumption."""

from .consumer import ContextArtifactConsumer, consume_once_if_armed
from .service import FillWatchContextOutcomeService

__all__ = [
    "ContextArtifactConsumer",
    "FillWatchContextOutcomeService",
    "consume_once_if_armed",
]
