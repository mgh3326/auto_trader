APPROVAL_KEY_CACHE_KEY = "kis:websocket:approval_key"
APPROVAL_KEY_TTL_SECONDS = 82800
RECOVERABLE_APPROVAL_MSG_CODES = {"OPSP0011", "OPSP8996"}

# Per-account-mode Redis namespaces for the WebSocket approval key.
# Live keeps the historical key (== APPROVAL_KEY_CACHE_KEY); mock is isolated so
# a mock approval key can never satisfy a live cache lookup or vice versa.
APPROVAL_KEY_CACHE_KEYS = {
    "kis_live": "kis:websocket:approval_key",
    "kis_mock": "kis_mock:websocket:approval_key",
}

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
