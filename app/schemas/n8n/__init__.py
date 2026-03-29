# app/schemas/n8n/__init__.py
"""n8n schemas package — re-exports all schemas for backward compatibility.

Prefer importing from submodules directly:
    from app.schemas.n8n.pending_orders import N8nPendingOrdersResponse
"""

from app.schemas.n8n.common import *  # noqa: F401,F403
from app.schemas.n8n.crypto_scan import *  # noqa: F401,F403
from app.schemas.n8n.daily_brief import *  # noqa: F401,F403
from app.schemas.n8n.filled_orders import *  # noqa: F401,F403
from app.schemas.n8n.kr_morning_report import *  # noqa: F401,F403
from app.schemas.n8n.market_context import *  # noqa: F401,F403
from app.schemas.n8n.pending_orders import *  # noqa: F401,F403
from app.schemas.n8n.pending_review import *  # noqa: F401,F403
from app.schemas.n8n.pending_snapshot import *  # noqa: F401,F403
from app.schemas.n8n.trade_review import *  # noqa: F401,F403
from app.schemas.n8n.news import *  # noqa: F401,F403
