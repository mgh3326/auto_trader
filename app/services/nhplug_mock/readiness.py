"""Explicit Stage 2 operator confirmations and blocking conditions."""

from __future__ import annotations

import os
from dataclasses import dataclass


class Stage2Disabled(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Stage2Readiness:
    key_confirmed: bool = False
    time_confirmed: bool = False
    db_confirmed: bool = False
    host_confirmed: bool = False
    vendor_confirmed: bool = False

    @classmethod
    def from_env(cls) -> Stage2Readiness:
        return cls(
            key_confirmed=os.getenv("NHPLUG_STAGE2_KEY_CONFIRMED") == "true",
            time_confirmed=os.getenv("NHPLUG_STAGE2_TIME_CONFIRMED") == "true",
            db_confirmed=os.getenv("NHPLUG_STAGE2_DB_CONFIRMED") == "true",
            host_confirmed=os.getenv("NHPLUG_STAGE2_HOST_CONFIRMED") == "true",
            vendor_confirmed=os.getenv("NHPLUG_STAGE2_VENDOR_CONFIRMED") == "true",
        )

    def assert_ready(self) -> None:
        if type(self) is not Stage2Readiness:
            raise Stage2Disabled("invalid_readiness")
        for name in ("key", "time", "db", "host", "vendor"):
            if getattr(self, f"{name}_confirmed") is not True:
                raise Stage2Disabled(f"{name}_unconfirmed")
