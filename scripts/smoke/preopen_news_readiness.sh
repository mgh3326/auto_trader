#!/usr/bin/env bash
# Read-only smoke check for /trading/decisions/preopen news readiness.
# Depends only on curl and jq. Does NOT post, ingest, or output secrets.
set -euo pipefail

BASE_URL="${PREOPEN_BASE_URL:-http://127.0.0.1:8000}"
ENDPOINT="${BASE_URL}/trading/decisions/preopen"

# Fetch response; -f fails on HTTP 4xx/5xx, -s silent, -S show errors
if ! response=$(curl -fsS --max-time 10 "$ENDPOINT" 2>/dev/null); then
    echo "ERROR: request to $ENDPOINT failed (non-200 or connection refused)"
    exit 1
fi

# Validate JSON and extract news slice
if ! slice=$(echo "$response" | jq -e '{
  news: .source_freshness.news,
  warnings: (.source_warnings // [] | map(select(startswith("news_"))))
}' 2>/dev/null); then
    echo "ERROR: response did not parse as expected JSON"
    exit 1
fi

warnings=$(echo "$slice" | jq -r '.warnings | join(", ")')

if [ -z "$warnings" ]; then
    echo "READY"
else
    echo "WARN: $warnings"
fi
