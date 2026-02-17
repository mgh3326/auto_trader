# CoinGecko `coins/list` Redis Cache Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `get_crypto_profile` 경로에서 CoinGecko `coins/list`를 Redis+메모리 2단 캐시로 전환해 외부 `coins/list` 호출 빈도를 줄인다.

**Architecture:** `app/mcp_server/tooling/fundamentals_sources_coingecko.py`의 `_get_coingecko_symbol_to_ids()`에 Redis read-through/write-through를 추가하고, 기존 in-memory cache와 asyncio lock을 유지한다. `coins/{id}` 상세 프로필 캐시는 변경하지 않으며, Redis 장애 시 원격 호출 fallback으로 기능을 유지한다.

**Tech Stack:** Python 3.13, `httpx`, `redis.asyncio`, pytest (`uv run pytest --no-cov`)

---

### Task 1: Redis Hit 경로를 먼저 테스트로 고정

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py`

**Step 1: Write the failing test**

`TestGetCryptoProfile`에 Redis hit 시 `coins/list` 원격 호출이 없어야 하는 테스트를 추가한다.

```python
async def test_get_crypto_profile_uses_redis_cached_coin_list(self, monkeypatch):
    tools = build_tools()
    self._reset_cache()

    class FakeRedis:
        async def get(self, key):
            if key == "coingecko:coins:list:v1":
                return json.dumps({"btc": ["bitcoin"]})
            return None

    monkeypatch.setattr(
        fundamentals_sources_coingecko,
        "_get_redis_client",
        AsyncMock(return_value=FakeRedis()),
    )

    class MockClient:
        async def get(self, url, params=None, **kw):
            if "/coins/list" in url:
                raise AssertionError("coins/list should not be called on redis hit")
            if "/coins/bitcoin" in url:
                return MockResponse({"name": "Bitcoin", "symbol": "btc", "market_data": {}})
            raise AssertionError(f"Unexpected URL: {url}")

    _patch_httpx_async_client(monkeypatch, MockClient)
    result = await tools["get_crypto_profile"]("BTC")
    assert result["symbol"] == "BTC"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py::TestGetCryptoProfile::test_get_crypto_profile_uses_redis_cached_coin_list -v`  
Expected: FAIL (`_get_redis_client` 미존재 또는 `coins/list` 호출 발생)

**Step 3: Write minimal implementation**

`fundamentals_sources_coingecko.py`에 Redis read helper를 추가한다.

```python
COINGECKO_LIST_REDIS_KEY = "coingecko:coins:list:v1"
COINGECKO_LIST_REDIS_TTL_SECONDS = 86400
_COINGECKO_REDIS_CLIENT: redis.Redis | None = None

async def _get_redis_client() -> redis.Redis:
    ...

def _validate_symbol_to_ids(payload: Any) -> dict[str, list[str]] | None:
    ...

async def _read_symbol_to_ids_from_redis() -> dict[str, list[str]] | None:
    ...
```

`_get_coingecko_symbol_to_ids()` 흐름에 Redis read-through를 추가한다.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py::TestGetCryptoProfile::test_get_crypto_profile_uses_redis_cached_coin_list -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py
git commit -m "test+feat: add redis read-through for coingecko coins list"
```

### Task 2: Redis Miss 시 write-through 및 payload 복구 경로 구현

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py`

**Step 1: Write the failing tests**

테스트 2개를 추가한다.

1. Redis miss 후 원격 `coins/list` 호출 결과를 Redis에 저장하는지
2. Redis payload가 손상된 JSON일 때 원격 재조회로 복구하는지

```python
async def test_get_crypto_profile_writes_coin_list_to_redis_on_miss(...):
    ...
    assert fake_redis.setex_calls[0][0] == "coingecko:coins:list:v1"
    assert fake_redis.setex_calls[0][1] == 86400

async def test_get_crypto_profile_ignores_invalid_redis_payload_and_refetches(...):
    ...
    assert list_calls["count"] == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py::TestGetCryptoProfile -k "writes_coin_list_to_redis_on_miss or ignores_invalid_redis_payload" -v`  
Expected: FAIL (`setex` 미호출 또는 payload 복구 실패)

**Step 3: Write minimal implementation**

Redis write helper를 추가하고 `_get_coingecko_symbol_to_ids()` 성공 경로에 연결한다.

```python
async def _write_symbol_to_ids_to_redis(symbol_to_ids: dict[str, list[str]]) -> None:
    payload = json.dumps(symbol_to_ids, separators=(",", ":"))
    await redis_client.setex(COINGECKO_LIST_REDIS_KEY, COINGECKO_LIST_REDIS_TTL_SECONDS, payload)
```

payload 검증은 엄격히 수행한다.

- key는 `str`
- value는 `list[str]` (빈 문자열 제외)
- 유효하지 않으면 캐시 미스로 처리

**Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py::TestGetCryptoProfile -k "uses_redis_cached_coin_list or writes_coin_list_to_redis_on_miss or ignores_invalid_redis_payload" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py
git commit -m "feat: add redis write-through and payload validation for coingecko list cache"
```

### Task 3: Redis 장애 fallback(원격 유지)과 회귀 검증 마무리

**Files:**
- Modify: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`
- Modify: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py`

**Step 1: Write the failing test**

Redis `get` 또는 `setex`에서 예외가 발생해도 `coins/list` 원격 호출로 정상 처리되는 테스트를 추가한다.

```python
async def test_get_crypto_profile_falls_back_when_redis_errors(self, monkeypatch):
    ...
    class BrokenRedis:
        async def get(self, key):
            raise RuntimeError("redis unavailable")
        async def setex(self, key, ttl, payload):
            raise RuntimeError("redis unavailable")
    ...
    result = await tools["get_crypto_profile"]("BTC")
    assert result["symbol"] == "BTC"
    assert list_calls["count"] == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py::TestGetCryptoProfile::test_get_crypto_profile_falls_back_when_redis_errors -v`  
Expected: FAIL (Redis 예외 전파)

**Step 3: Write minimal implementation**

Redis read/write helper에 예외 보호를 추가한다.

```python
try:
    payload = await redis_client.get(COINGECKO_LIST_REDIS_KEY)
except Exception as exc:
    logger.warning("coingecko_list_cache_redis_error stage=read error=%s", exc)
    return None
```

```python
try:
    await redis_client.setex(...)
except Exception as exc:
    logger.warning("coingecko_list_cache_redis_error stage=write error=%s", exc)
```

주의: CoinGecko 원격 호출 실패 예외는 잡지 않고 그대로 전파한다 (stale fallback 금지 요구사항).

**Step 4: Run verification suite**

Run:

1. `uv run pytest --no-cov tests/test_mcp_server_tools.py::TestGetCryptoProfile -v`  
Expected: PASS
2. `uv run ruff check app/mcp_server/tooling/fundamentals_sources_coingecko.py tests/test_mcp_server_tools.py`  
Expected: All checks passed
3. `uv run pyright app/mcp_server/tooling/fundamentals_sources_coingecko.py`  
Expected: 0 errors

**Step 5: Commit**

```bash
git add /Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py /Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py
git commit -m "fix: keep coingecko coin-list path resilient on redis failures"
```

### Task 4: 구현 완료 후 관측 체크리스트

**Files:**
- Reference: `/Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-coingecko-coins-list-redis-cache-design.md`

**Step 1: 배포 전 기준선 확보**

Sentry에서 최근 24시간 `coins/list` span 빈도를 기록한다.

Query 예시:

```text
transaction:"tools/call get_crypto_profile" span.op:http.client span.description:"GET https://api.coingecko.com/api/v3/coins/list"
```

**Step 2: 배포 후 동일 기간 비교**

동일 쿼리로 배포 후 24시간 데이터를 비교한다.

**Step 3: 성공/실패 판정**

- 성공: `coins/list` span 발생 횟수 감소
- 실패: 감소가 미미하면 Redis hit 로그 비율과 TTL 설정/키 적중 여부를 재검증

**Step 4: 운영 로그 점검**

아래 이벤트의 비율을 확인한다.

- `coingecko_list_cache_memory_hit`
- `coingecko_list_cache_redis_hit`
- `coingecko_list_cache_remote_fetch`
- `coingecko_list_cache_redis_error`

**Step 5: Commit (optional docs note)**

관측 결과를 운영 노트에 남길 경우:

```bash
git add /Users/robin/PycharmProjects/auto_trader/docs/plans/2026-02-17-coingecko-coins-list-redis-cache-design.md
git commit -m "docs: record coingecko list-cache rollout observations"
```
