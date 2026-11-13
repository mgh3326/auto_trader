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
  A KIS forced invalidation is refused before the in-process copy is cleared;
  that copy is revalidated against Redis on the existing interval.
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
keys as the API process. Python performs the following fail-closed sequence:

1. Read the existing Redis token.
2. On a miss or expiry buffer hit, POST gatewayd's provider ensure endpoint.
3. Re-read Redis with a short bounded poll and use only a valid published token.

Python never takes a provider issuance lock in `gatewayd` mode. gatewayd owns
the OAuth leg and serializes concurrent ensures with that lock; taking it in
the API process before the ensure request would deadlock a cache miss.

The endpoints are `/v1/tokens/kis-live/ensure`, `/v1/tokens/kis-mock/ensure`,
and `/v1/tokens/toss/ensure`. A gatewayd transport failure, non-2xx response,
or an acknowledgement without a usable Redis token raises an issuance error;
Python must not fall back to its own OAuth endpoint in this mode.

For a Mac API runtime, set `GATEWAYD_URL` to the tailnet-reachable gatewayd URL;
the default loopback address only works when gatewayd is local to that process.

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
and secret are OAuth-only here, so they may be removed from the API environment
only after gatewayd mode has stabilized and the accompanying Redis namespace
configuration supports that operational change.
