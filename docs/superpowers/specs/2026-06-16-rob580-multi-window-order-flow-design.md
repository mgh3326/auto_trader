# Design Spec: Multi-Window Crypto Order Flow (ROB-580)

## 1. Problem Statement
The current `get_crypto_order_flow` tool provides only a single snapshot (default 200 ticks) of volume-weighted net flow. This is prone to noise and "whale" trade distortion, leading to conflicting conclusions between different agents or across short time intervals (e.g., BTC flipping from -0.8 to +0.9 in minutes).

## 2. Proposed Solution
Upgrade the tool to return multiple analysis windows (50, 200, 500 ticks) derived from a single 500-tick fetch. Add "Disjoint Segment Analysis" (comparing the last 50 ticks vs. the 450 ticks before that) to detect true acceleration, deceleration, or reversal. Implement a deadband and whale guard to filter noise.

## 3. Architecture & Data Flow
- **Single Source**: Fetch 500 ticks from Upbit `/v1/trades/ticks`.
- **Transformation**: 
    - Windows: 50, 200, 500 (Last N).
    - Disjoint Segments: `recent` (0:50) vs `older` (50:500).
- **Calculation**:
    - `net` = (Buy Vol - Sell Vol) / Total Vol.
    - `largest_trade_share` = Max(Tick Vol) / Total Vol.
    - `span_seconds` = First Trade Time - Last Trade Time.
- **Consensus Heuristics**:
    - Deadband ($\epsilon = 0.10$): Values below this are neutral.
    - Trend: `strengthening`, `weakening`, `stable`, `reversing`, or `neutral` based on `recent` vs `older` sign and magnitude.
    - Confidence: Downgraded to `low` if trade density is low or a single trade dominates (>35% of 50-tick window).

## 4. Response Schema
```json
{
  "symbol": "KRW-BTC",
  "as_of": "ISO-TIMESTAMP",
  "source": "upbit",
  "default_window": 200,
  "net": 0.0,
  "buy_ratio": 0.5,
  "windows": {
    "50":  { "net": 0.0, "buy_ratio": 0.5, "trade_count": 50, "span_seconds": 120, "largest_trade_share": 0.05 },
    "200": { "net": 0.0, "buy_ratio": 0.5, "trade_count": 200, "span_seconds": 450, "largest_trade_share": 0.02 },
    "500": { "net": 0.0, "buy_ratio": 0.5, "trade_count": 480, "span_seconds": 1200, "largest_trade_share": 0.01 }
  },
  "consensus": {
    "direction": "buy|sell|mixed|neutral",
    "agreement": true|false,
    "trend": "strengthening_up|weakening_up|reversing_up|stable_up|...",
    "confidence": "normal|low",
    "note": "Human-readable summary of divergence or consensus"
  }
}
```

## 5. Testing Strategy
- **Unit Tests**:
    - Verify disjoint calculation logic with mocked trade arrays.
    - Verify deadband $\epsilon$ behavior.
    - Verify whale guard (largest_trade_share) confidence downgrade.
    - Verify window derivation from a single array.
- **Integration Tests**:
    - Ensure `fetch_recent_trades` is called once with 500 count.
