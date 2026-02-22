from __future__ import annotations

import sys

from app.services.brokers.kis import client as _kis_client

sys.modules[__name__] = _kis_client
