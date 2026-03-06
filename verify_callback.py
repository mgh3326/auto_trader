#!/usr/bin/env python
"""Verification script for error callback registration."""
import asyncio
from app.services.kr_hourly_candles_read_service import _log_task_exception

task = asyncio.create_task(asyncio.sleep(0))
task.add_done_callback(_log_task_exception)
print("Error callback registered")
