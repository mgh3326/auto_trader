from __future__ import annotations

import sys

from app.services.brokers.yahoo import client as _yahoo_client

sys.modules[__name__] = _yahoo_client
