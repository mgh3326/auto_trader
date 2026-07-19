# US Dual-Paper Premarket Preview Runbook

## 1. Purpose & Safety
This runbook covers the default-disabled, read-only US Dual-Paper premarket preview/preflight path (ROB-326).
- **Environment:** Strictly paper/mock brokers only (`alpaca_paper` + `kis_mock`).
- **Read-Only Gate:** This path contains **absolutely no submission, cancellation, or modification logic**. The `submit_enabled` flag inside generated preview packets is strictly hardcoded to `false` and is not bypassable.
- **Disclaimer:** This tool is purely for preflight diagnostic validation and is **not a live-trading recommendation**.

---

## 2. Enablement & Environment Variables
To opt into the premarket preview, set the opt-in flag and configure credentials for both brokers:
- `US_DUAL_PAPER_PREVIEW_ENABLED=true` (opt-in; default `false`)
- **Alpaca Paper Credentials:**
  - `ALPACA_PAPER_API_KEY`
  - `ALPACA_PAPER_API_SECRET`
- **KIS Mock Credentials:**
  - `KIS_MOCK_ENABLED=true`
  - `KIS_MOCK_APP_KEY`
  - `KIS_MOCK_APP_SECRET`
  - `KIS_MOCK_ACCOUNT_NO`

---

## 3. Preflight Checks
Before generating previews, run the preflight verification command:
```bash
US_DUAL_PAPER_PREVIEW_ENABLED=true uv run python -m scripts.smoke.us_dual_paper_preview_smoke --mode preflight
```

### Exit Code Interpretation:
- `0`: Success (or disabled no-op if `US_DUAL_PAPER_PREVIEW_ENABLED` is not set). Both brokers are fully configured and ready.
- `1`: Config or credential problem. One or both adapters reported `missing_env_keys` (printed by env name only — secrets are never logged).
- `2`: Operational or runtime failure.

---

## 4. Preview Execution
Generate a preview packet for 1–3 US stock symbols:
```bash
US_DUAL_PAPER_PREVIEW_ENABLED=true uv run python -m scripts.smoke.us_dual_paper_preview_smoke \
  --mode preview --symbol NVDA --quantity 1 --limit-price 10.0 --notional-cap 50.0
```

### Response Semantics:
Each broker (`alpaca_paper` and `kis_mock`) is evaluated independently and reports one of the following statuses:
1. `previewed`: The BUY/LIMIT order successfully passed all broker limits, including local notional caps and buying power checks.
2. `blocked`: The order violates a constraint:
   - Notional cost exceeds the operator-defined `--notional-cap` (e.g. `notional_exceeds_cap`).
   - Order cost exceeds available broker buying power (e.g. `insufficient_buying_power`, `would_exceed_buying_power`).
   - Order parameters are invalid (e.g. `quantity_must_be_positive`).
   - Limit price deviates excessively (> 10%) from the reference price (e.g. `limit_price_deviation_exceeds_bound`).
3. `unsupported`: The broker is not configured due to missing environment variables.
4. `error`: A runtime exception occurred (e.g. connection error) while checking the broker. **Failure at one broker never disrupts or crashes the check for the other broker.**

### Known KIS mock limitations (verified by live smoke 2026-05-27)

한국투자증권 **모의투자(mock)** does not expose the full overseas account surface. These are
broker-side limits, not defects, and the preview handles them gracefully:

- **USD cash / buying-power reads via VTTS3007R (ROB-951).** The overseas foreign-margin
  service still returns `OPSQ0002 없는 서비스 코드` in mock, but `kis_mock` no longer relies
  on it — it queries the same mock-only orderable-cash TR (`VTTS3007R`,
  `inquire_mock_overseas_buyable_amount`) the order preflight gate uses, and reports
  `cash_usd` / `buying_power_usd` as the parsed `ord_psbl_frcr_amt` value. The KIS-side
  buying-power check now **runs**: a `kis_mock` `previewed` status means caps + parameter
  checks **and** confirmed available USD funds passed. `cash_usd: null` /
  `buying_power_usd: null` still occurs — but only when the VTTS3007R call fails, returns a
  non-zero `rt_cd`, or omits the expected field — and is treated as fail-closed
  "balance unknown" (never assumed to be `0` or unlimited); confirm KIS funds manually in
  that case before any (separate, confirm-gated) submit.
- **Overseas holdings/positions DO read** on the mock host (`openapivts`), so
  `position_count` is populated.
- **Overseas pending-orders inquiry is blocked** in mock (`TTTS3018R` not available), so
  `open_order_count` is `null` for `kis_mock`.
- **Client host matters:** mock reads only succeed through a `KISClient(is_mock=True)`
  instance (mock host). Sending a mock TR through the live-host singleton returns
  `EGW02005 실전투자 TR 이 아닙니다`. The adapter constructs the mock-host client correctly;
  do not route `kis_mock` reads through the live `kis` singleton.

`alpaca_paper` has no such limits — cash, buying-power, positions, and open orders all read.

---

## 5. Model Context Protocol (MCP) Equivalent
For AI or agentic workers, three read-only tools are exposed on the MCP server:
- `us_dual_paper_capability_matrix`: Fetch secret-free capabilities metadata.
- `us_dual_paper_account_states`: Read cash, positions, and buying power securely (counts/numbers only).
- `us_dual_paper_preview`: Build the dual-broker BUY/LIMIT preview packet.

---

## 6. Manual Operator Review Checklist
When inspecting preview packets, confirm the following:
1. `limit_price_source`: Verify the source of the limit price (e.g., `operator_input`).
2. `notional_cap_usd`: Confirm that the cap matches your intended max exposure per trade.
3. `account_state.buying_power_usd`: Verify buying power where available. **`alpaca_paper`** reports a number; **`kis_mock`** reports the VTTS3007R-derived orderable USD amount (see §4) and is now enforced by the buying-power check — a `kis_mock` `null` means the balance read failed and is fail-closed "unknown", not "unlimited"; confirm KIS funds manually in that case before any submit.

---

## 7. 22:30 KST Regular-Session Handoff
> [!IMPORTANT]
> The premarket preview path is **strictly read-only** and does not support submission.
> To proceed to regular-session paper-trading execution at or after 22:30 KST:

1. **Submit Orders:** Use the separate, confirm-gated broker submission tools:
   - Alpaca Paper: `alpaca_paper_submit_order` MCP tool.
   - KIS Mock: Existing KIS mock execution endpoints.
2. **Review Confirm Gates:** Ensure that the required confirm flags (e.g. `--confirm` or `confirm_fill`) are set to true to execute.
3. **Start Small:** Always submit a single, small-lot size order first (e.g. 1 share of a highly liquid stock like `AAPL` or `NVDA`) to verify connection.
4. **Monitor Fills:** Inspect the transaction log or broker ledger to confirm the order was filled correctly.
5. **Rollback / Cancel Steps:** If an anomaly or incorrect price execution occurs, cancel all open orders immediately using the cancel tools:
   - Alpaca Paper: `alpaca_paper_cancel_order` or bulk cancellation.
   - KIS Mock: KIS mock cancellation endpoint.

---

## 8. Stale Quote Sanity Rule
- **Quote Freshness:** If no fresh reference price is supplied, the preview system will emit a warning:
  `reference_price_missing_for_limit_sanity`
- **Execution Block:** In the event of a missing or stale quote, the operator **MUST NOT** proceed to manual regular-session submission. Execution is strictly blocked until a fresh quote is obtained.
