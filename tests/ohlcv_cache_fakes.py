# tests/ohlcv_cache_fakes.py
"""Shared fake Redis implementation for OHLCV cache tests."""
from __future__ import annotations

from typing import Any


class FakePipeline:
    def __init__(self, redis_client: FakeRedis):
        self.redis_client = redis_client
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def zremrangebyrank(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("zremrangebyrank", args, kwargs))
        return self

    def hdel(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("hdel", args, kwargs))
        return self

    def zadd(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("zadd", args, kwargs))
        return self

    def hset(self, *args: Any, **kwargs: Any) -> FakePipeline:
        self.commands.append(("hset", args, kwargs))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for method_name, args, kwargs in self.commands:
            method = getattr(self.redis_client, method_name)
            results.append(await method(*args, **kwargs))
        self.commands.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        del transaction
        return FakePipeline(self)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def eval(
        self,
        script: str,
        key_count: int,
        key: str,
        token: str,
    ) -> int:
        del script, key_count
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0

    async def zadd(
        self, key: str, mapping: dict[str, int | float]
    ) -> int:
        zset = self.zsets.setdefault(key, {})
        inserted = 0
        for member, score in mapping.items():
            if member not in zset:
                inserted += 1
            zset[member] = float(score)
        return inserted

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zcount(
        self,
        key: str,
        minimum: str | int | float,
        maximum: str | int | float,
    ) -> int:
        zset = self.zsets.get(key, {})
        min_score = self._normalize_score(minimum, is_min=True)
        max_score = self._normalize_score(maximum, is_min=False)
        return sum(
            1 for score in zset.values() if min_score <= score <= max_score
        )

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
        )
        members = [member for member, _ in items]
        if not members:
            return []
        if end < 0:
            end = len(members) + end
        if end < start:
            return []
        return members[start : end + 1]

    async def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        members = [member for member, _ in items]
        if not members:
            return []
        if end < 0:
            end = len(members) + end
        if end < start:
            return []
        return members[start : end + 1]

    async def zrevrangebyscore(
        self,
        key: str,
        maximum: str | int | float,
        minimum: str | int | float,
        start: int = 0,
        num: int | None = None,
    ) -> list[str]:
        zset = self.zsets.get(key, {})
        min_score = self._normalize_score(minimum, is_min=True)
        max_score = self._normalize_score(maximum, is_min=False)
        items = [
            (member, score)
            for member, score in zset.items()
            if min_score <= score <= max_score
        ]
        items.sort(key=lambda item: (item[1], item[0]), reverse=True)
        members = [member for member, _ in items]
        if num is None:
            return members[start:]
        return members[start : start + num]

    async def zremrangebyrank(
        self, key: str, start: int, end: int
    ) -> int:
        members = await self.zrange(key, 0, -1)
        if not members:
            return 0
        if end < 0:
            end = len(members) + end
        if end < start:
            return 0
        removable = members[start : end + 1]
        zset = self.zsets.get(key, {})
        for member in removable:
            zset.pop(member, None)
        return len(removable)

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        target = self.hashes.setdefault(key, {})
        inserted = 0
        for field, value in mapping.items():
            if field not in target:
                inserted += 1
            target[field] = value
        return inserted

    async def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        target = self.hashes.get(key, {})
        return [target.get(field) for field in fields]

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        target = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in target:
                removed += 1
                target.pop(field, None)
        return removed

    @staticmethod
    def _normalize_score(
        value: str | int | float, is_min: bool
    ) -> float:
        if isinstance(value, str):
            if value == "-inf":
                return float("-inf")
            if value == "+inf":
                return float("inf")
        parsed = float(value)
        if parsed == float("-inf") and not is_min:
            return float("-inf")
        if parsed == float("inf") and is_min:
            return float("inf")
        return parsed
