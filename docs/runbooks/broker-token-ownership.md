# Broker token issuance ownership

## Default state

`BROKER_TOKEN_ISSUANCE_MODE=self` is the default and preserves the existing
Python OAuth issuance flow for KIS and Toss. `GATEWAYD_URL` defaults to
`http://127.0.0.1:8791` and has no effect in `self` mode.

## cache_only mode

`BROKER_TOKEN_ISSUANCE_MODE=cache_only` makes this process a **read-only**
consumer of the shared Redis token cache. It reads the existing key of each
provider path and never issues a token itself. It is meant for research jobs,
scripts, and local hosts that need broker tokens but must not issue them.

- **Usable cached token**: KIS live and mock access tokens, the Toss OAuth token,
  and KIS WebSocket approval keys return the cached value exactly as in `self`
  mode. There is no issuance side effect.
- **Miss, expired token, or forced re-issue**: KIS `clear_token`, Toss
  `force_reissue`, and the approval-key `invalidate_and_reissue` path raise
  `TokenIssuanceCacheOnlyUnavailable`, a subclass of `TokenIssuanceUnavailable`,
  and stop there. They make no OAuth or approval HTTP request, no gatewayd
  ensure request, take no issuance lock, and write or delete no Redis key.
  A KIS forced invalidation never writes or deletes the Redis key. It drops this
  process's in-process copy and remembers the rejected token, so Redis does not
  hand that same token back. The next token the producer publishes to Redis is
  used. Until the producer rotates, requests that read the cache get a miss and
  are refused.
- The error message names the provider and the mode only. It never contains a
  token, key, URL, or Redis key name.

Toss allows one valid token per client. A `self` process pointed at a different
Redis issues its own token and invalidates the token the production processes
share. So `self` is used only on the single host whose Redis is the shared one.
Every other process that only needs the shared token runs in `cache_only`.

The default stays `self`. `cache_only` does not replace `gatewayd`: a stale or
missing cache in this mode fails the request until the producer publishes a new
token.

## gatewayd mode

Set `BROKER_TOKEN_ISSUANCE_MODE=gatewayd` only after gatewayd is deployed with
the matching broker credentials and its Redis access targets the same token
keys as the API process. For KIS WebSocket approval keys, gatewayd must
additionally run with the `kis-live-approval` and `kis-mock-approval`
providers enabled before a process switches to `gatewayd` mode; without them
the approval ensure endpoint answers 404 and approval-key reads fail closed.
Python performs the following fail-closed sequence:

1. Read the existing Redis token.
2. On a miss or expiry buffer hit, POST gatewayd's provider ensure endpoint.
3. Re-read Redis with a short bounded poll and use only a valid published token.

Python never takes a provider issuance lock in `gatewayd` mode. gatewayd owns
the OAuth leg and serializes concurrent ensures with that lock; taking it in
the API process before the ensure request would deadlock a cache miss.

KIS WebSocket approval keys follow the same sequence. `get_approval_key`
reads the cached key and, on a miss, POSTs
`/v1/tokens/kis-live-approval/ensure` or
`/v1/tokens/kis-mock-approval/ensure` (chosen by account mode) and re-reads
the shared `kis:websocket:approval_key` /
`kis_mock:websocket:approval_key` cache with the same bounded poll.
`invalidate_and_reissue_approval_key` (the OPSP0011 controlled reissue) posts
the same endpoint with `?force=true`, which makes gatewayd replace the
published key without a no-key window. In `gatewayd` mode Python never calls
the KIS approval endpoint itself and never takes the approval-key issuance
lock.

The endpoints are `/v1/tokens/kis-live/ensure`, `/v1/tokens/kis-mock/ensure`,
`/v1/tokens/toss/ensure`, `/v1/tokens/kis-live-approval/ensure`, and
`/v1/tokens/kis-mock-approval/ensure`. A gatewayd transport failure, non-2xx
response, or an acknowledgement without a usable Redis token raises an
issuance error; Python must not fall back to its own OAuth or approval
endpoint in this mode.

For a Mac API runtime, set `GATEWAYD_URL` to the tailnet-reachable gatewayd URL;
the default loopback address only works when gatewayd is local to that process.

### Toss 소비자에서 OAuth 자격 증명 제거

`gatewayd` 또는 `cache_only` 모드에서 Toss 소비자는 Redis 키의 네임스페이스만
알면 됩니다. 최초 한 번, Toss client ID가 설정된 환경에서 다음 명령을 실행해
fp16만 출력합니다:

```bash
uv run python scripts/print_toss_token_namespace_fp.py
```

출력값을 소비자 환경의 `TOSS_TOKEN_NAMESPACE_FP`에 설정합니다. 이 값은
Toss client ID의 SHA-256 앞 16자리 16진수이며 비밀값이 아닙니다. `gatewayd`와
`cache_only` 소비자는 이 값만으로 기존 Redis 토큰 키를 사용합니다. 운영자가
소비자 환경에서 `TOSS_API_CLIENT_ID`와 `TOSS_API_CLIENT_SECRET`을 제거할 수
있습니다. 환경 변경은 운영자가 수행하며 gatewayd의 발급 자격 증명은 유지합니다.

## Cutover and rollback

1. Confirm gatewayd can issue each intended provider token and publishes the
   Python-compatible Redis record.
2. Deploy this code while the mode remains `self`.
3. Change `BROKER_TOKEN_ISSUANCE_MODE` to `gatewayd`, restart the API process,
   and verify one token miss causes gatewayd ensure followed by a Redis hit.
4. If gatewayd is unhealthy, return the mode to `self` through the operator
   configuration change; do not add runtime fallback.

KIS API key and secret remain in the API environment: KIS requires them on
every API request header even when gatewayd owns OAuth issuance. Toss client ID
and secret are OAuth-only here, so an operator may remove them from consumer
environments after gatewayd mode has stabilized and `TOSS_TOKEN_NAMESPACE_FP`
is configured with the matching Redis namespace.
