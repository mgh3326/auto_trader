# n8n API Reference

This document provides a comprehensive reference for all n8n-specific API endpoints available in the Auto Trader system. These endpoints are designed for integration with n8n workflows, Discord notifications, and automated trading reports.

## Authentication

All endpoints require an API key passed in the `X-N8N-API-Key` header.

- **Header:** `X-N8N-API-Key: <your_api_key>`
- **Environment Variable:** `N8N_API_KEY`

---

## Endpoints Summary

| Category | Method | Path | Description |
| :--- | :--- | :--- | :--- |
| **Market Data** | GET | `/api/n8n/pending-orders` | List current pending orders across markets |
| | GET | `/api/n8n/market-context` | Technical indicators for specific symbols |
| | GET | `/api/n8n/daily-brief` | Unified report for daily Discord updates |
| | GET | `/api/n8n/filled-orders` | List recently filled orders |
| **Crypto Scan** | GET | `/api/n8n/crypto-scan` | Advanced coin scanner (RSI, SMA, Crash) |
| | GET | `/api/n8n/scan/strategy` | Run strategy-based signal detection |
| | GET | `/api/n8n/scan/crash` | Run rapid price movement detection |
| **Trade Reviews**| POST | `/api/n8n/trade-reviews` | Save execution reviews from n8n |
| | GET | `/api/n8n/trade-reviews` | List saved trade reviews with filters |
| | GET | `/api/n8n/trade-reviews/stats` | Aggregate performance statistics |
| **Snapshots** | GET | `/api/n8n/pending-review` | List pending orders for manual review |
| | POST | `/api/n8n/pending-snapshots` | Save snapshots of pending orders |
| | PATCH| `/api/n8n/pending-snapshots/resolve` | Mark snapshots as filled/cancelled |

---

## Market Data

### GET `/api/n8n/pending-orders`
List all current pending (unfilled) orders with optional technical indicator enrichment.

**Query Parameters:**
- `market`: (optional) `crypto`, `kr`, `us`, or `all` (default)
- `min_amount`: (optional) Minimum KRW amount filter (default: 0)
- `include_current_price`: (optional) Boolean, compute gap from current price (default: true)
- `side`: (optional) `buy` or `sell`
- `include_indicators`: (optional) Boolean, include RSI/ADX/EMA data (default: true)

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/pending-orders?market=crypto"
```

### GET `/api/n8n/market-context`
Fetch technical context (RSI, trend, ADX) for specific symbols or current holdings.

**Query Parameters:**
- `market`: (optional) `crypto` (default), `kr`, `us`, or `all`
- `symbols`: (optional) Comma-separated list (e.g., `BTC,ETH,005930`)
- `include_fear_greed`: (optional) Boolean (default: true)
- `include_economic_calendar`: (optional) Boolean (default: true)

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/market-context?symbols=BTC,ETH"
```

### GET `/api/n8n/daily-brief`
Unified endpoint providing pending orders, portfolio summary, and yesterday's fills for a daily Discord report.

**Query Parameters:**
- `markets`: (optional) Comma-separated list, e.g., `crypto,kr,us`
- `min_amount`: (optional) Minimum KRW amount filter (default: 50,000)

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/daily-brief"
```

### GET `/api/n8n/filled-orders`
List recently executed orders.

**Query Parameters:**
- `days`: (optional) Lookback period in days (default: 1)
- `markets`: (optional) Comma-separated list (default: `crypto,kr,us`)
- `min_amount`: (optional) Minimum filled amount filter

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/filled-orders?days=7"
```

---

## Crypto Scan

### GET `/api/n8n/crypto-scan`
Advanced scan for crypto markets, filtering by trade volume and detecting technical signals.

**Query Parameters:**
- `top_n`: (optional) Number of coins by 24h trade volume (default: 30)
- `include_holdings`: (optional) Include current holding coins (default: true)
- `include_crash`: (optional) Run crash detection (default: true)
- `include_sma_cross`: (optional) Run SMA cross detection (default: true)
- `ohlcv_days`: (optional) Lookback for indicators (default: 50)

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/crypto-scan?top_n=50"
```

### GET `/api/n8n/scan/strategy`
Trigger a strategy-based scan and return text-based signal summaries.

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/scan/strategy"
```

### GET `/api/n8n/scan/crash`
Trigger a rapid price movement (crash) detection scan.

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/scan/crash"
```

---

## Trade Reviews

### POST `/api/n8n/trade-reviews`
Save trade reviews and snapshots sent from n8n.

**Request Body:**
```json
{
  "reviews": [
    {
      "order_id": "abc-123",
      "account": "upbit",
      "symbol": "BTC",
      "instrument_type": "crypto",
      "side": "buy",
      "price": 98000000,
      "quantity": 0.015,
      "total_amount": 1470000,
      "verdict": "good",
      "comment": "RSI oversold entry",
      "review_type": "daily",
      "filled_at": "2026-03-17T14:30:00+09:00",
      "indicators": { "rsi_14": 31.2 }
    }
  ]
}
```

**Example:**
```bash
curl -X POST -H "Content-Type: application/json" -H "X-N8N-API-Key: secret" \
  -d '{"reviews": [...]}' "http://localhost:8000/api/n8n/trade-reviews"
```

### GET `/api/n8n/trade-reviews`
Query saved trade reviews with optional filters.

**Query Parameters:**
- `period`: (optional) `7d`, `30d`, `90d` (default: `7d`)
- `market`: (optional) `crypto`, `kr`, `us`
- `symbol`: (optional) Normalized symbol (e.g., `BTC`)
- `limit`: (optional) Max results to return (default: 100)

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/trade-reviews?period=30d&market=crypto"
```

### GET `/api/n8n/trade-reviews/stats`
Get aggregate performance stats for a specific period.

**Query Parameters:**
- `period`: (optional) `week`, `month`, `quarter` (default: `week`)
- `market`: (optional) Filter by market type

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/trade-reviews/stats?period=month"
```

---

## Snapshots & Resolutions

### GET `/api/n8n/pending-review`
List pending orders with "Fill Probability" and suggestions for manual review.

**Example:**
```bash
curl -H "X-N8N-API-Key: secret" "http://localhost:8000/api/n8n/pending-review"
```

### POST `/api/n8n/pending-snapshots`
Save snapshots of current pending orders to track their lifecycle.

**Example:**
```bash
curl -X POST -H "Content-Type: application/json" -H "X-N8N-API-Key: secret" \
  -d '{"snapshots": [...]}' "http://localhost:8000/api/n8n/pending-snapshots"
```

### PATCH `/api/n8n/pending-snapshots/resolve`
Update the status of saved snapshots (filled, cancelled, or expired).

**Example:**
```bash
curl -X PATCH -H "Content-Type: application/json" -H "X-N8N-API-Key: secret" \
  -d '{"resolutions": [...]}' "http://localhost:8000/api/n8n/pending-snapshots/resolve"
```
