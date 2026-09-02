# Broker token issuance ownership

## Default state

`BROKER_TOKEN_ISSUANCE_MODE=self` is the default and preserves the existing
Python OAuth issuance flow for KIS and Toss. `GATEWAYD_URL` defaults to
`http://127.0.0.1:8791` and has no effect in `self` mode.

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
