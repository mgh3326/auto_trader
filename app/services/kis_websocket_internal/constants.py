APPROVAL_KEY_CACHE_KEY = "kis:websocket:approval_key"
APPROVAL_KEY_TTL_SECONDS = 82800
# Codes that mean "the approval key we sent was rejected; reissue may recover".
# OPSP8996 ("ALREADY IN USE appkey") is deliberately NOT here: it signals
# websocket session occupancy by another owner and is handled by a dedicated
# fail-fast/long-backoff branch (client.KISAppKeyInUseError) that must never
# reissue/churn the approval key. Keeping it out of this set keeps the
# reissue path honest. (ROB-262)
RECOVERABLE_APPROVAL_MSG_CODES = {"OPSP0011"}

# Per-account-mode Redis namespaces for the WebSocket approval key.
# Live keeps the historical key (== APPROVAL_KEY_CACHE_KEY); mock is isolated so
# a mock approval key can never satisfy a live cache lookup or vice versa.
APPROVAL_KEY_CACHE_KEYS = {
    "kis_live": "kis:websocket:approval_key",
    "kis_mock": "kis_mock:websocket:approval_key",
}

# Single-flight issuance lock (ROB-262). Only the lock holder may call the KIS
# approval endpoint during a cold-issuance window; contenders wait briefly and
# reuse the cached key. Namespaced per account mode so live/mock never contend.
APPROVAL_KEY_LOCK_CACHE_KEYS = {
    "kis_live": "kis:websocket:approval_key:lock",
    "kis_mock": "kis_mock:websocket:approval_key:lock",
}
# Lock TTL must outlast the approval HTTP round-trip (10s client timeout) + the
# cache write, yet be short enough to self-heal if the holder process dies.
APPROVAL_KEY_LOCK_TTL_SECONDS = 15
# Bounded wait a contender will spend re-reading Redis for the holder's key
# before failing/backing off. Kept STRICTLY UNDER the lock TTL so a dead holder's
# lock expires rather than deadlocking the next cold-issuance window. This is safe
# because a live holder's work is hard-capped at ~10s (the approval HTTP call uses
# httpx timeout=10 with no retries, then a sub-ms Redis cache write), leaving the
# 12s contender a ~2s margin to observe the published key. Never raise this above
# APPROVAL_KEY_LOCK_TTL_SECONDS — that would re-introduce prolonged blocking on a
# dead holder.
APPROVAL_KEY_WAIT_TIMEOUT_SECONDS = 12.0
# Base poll interval between contender re-reads; jitter is added on top.
APPROVAL_KEY_WAIT_POLL_SECONDS = 0.25

# Transport-layer host allowlists (fail-closed). Two layers:
#   1. approval endpoint (REST /oauth2/Approval) — issues the approval key
#   2. websocket endpoint (real-time stream) — consumes the approval key
# Each account mode may only ever touch its own host:port pair.
APPROVAL_ENDPOINT_HOSTS = {
    "kis_live": "openapi.koreainvestment.com:9443",
    "kis_mock": "openapivts.koreainvestment.com:29443",
}
WEBSOCKET_ENDPOINT_HOSTS = {
    "kis_live": "ops.koreainvestment.com:21000",
    "kis_mock": "ops.koreainvestment.com:31000",
}
