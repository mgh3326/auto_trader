from __future__ import annotations

import sys

from app.services.brokers.upbit import client as _upbit_client

sys.modules[__name__] = _upbit_client
