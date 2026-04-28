# KIS Account/Order Lifecycle TR Routing Matrix

This document is the canonical reference for KIS REST TR IDs used across the
account and order lifecycle for both domestic (KR) and overseas (US) markets.

**References:**
- Live TR constants: `app/services/brokers/kis/constants.py`
- Caller behavior for mock-unsupported endpoints: `app/mcp_server/README.md` §"KIS mock unsupported endpoints"
- Regression guard: `tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented`

---

## KR Domestic Endpoints

| endpoint | lifecycle_stage | kis_url | tr_live | tr_mock | is_mock=True behavior | surface error tag | pinned_by_test |
|---|---|---|---|---|---|---|---|
| `inquire_domestic_balance` (via `fetch_my_stocks`) | holdings/balance | `/uapi/domestic-stock/v1/trading/inquire-balance` | `TTTC8434R` | `VTTC8434R` | uses mock TR | — | `tests/test_kis_account_fetch_stocks.py` |
| `inquire_domestic_cash_balance` | cash/orderable | `/uapi/domestic-stock/v1/trading/inquire-balance` | `TTTC8434R` | `VTTC8434R` | uses mock TR | — | `tests/test_portfolio_cash_kis_mock.py::test_cash_balance_mock_uses_domestic_cash_not_integrated_margin` |
| `inquire_integrated_margin` | cash/orderable | `/uapi/domestic-stock/v1/trading/intgr-margin` | `TTTC0869R` | **mock_unsupported** | fails closed (raises `RuntimeError` with `"mock"` in message) | `errors[].mock_unsupported=true` | `tests/test_kis_integrated_margin_mock.py::test_integrated_margin_mock_fails_closed` |
| `inquire_korea_orders` | pending-inquiry, cancel/modify-lookup | `/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl` | `TTTC8036R` | **mock_unsupported** | fails closed (raises `RuntimeError` with `"mock"` in message) | `errors[].mock_unsupported=true` | `tests/test_kis_domestic_pending_mock.py::test_inquire_korea_orders_mock_fails_closed` |
| `inquire_daily_order_domestic` | daily-history | `/uapi/domestic-stock/v1/trading/inquire-daily-ccld` | `TTTC8001R` | `VTTC8001R` | uses mock TR | — | `tests/test_kis_mock_routing.py::test_inquire_daily_order_mock_uses_mock_tr[domestic]` |
| `order_korea_stock` (buy) | order-submit | `/uapi/domestic-stock/v1/trading/order-cash` | `TTTC0012U` | `VTTC0012U` | uses mock TR | — | `tests/test_kis_order_ops.py` |
| `order_korea_stock` (sell) | order-submit | `/uapi/domestic-stock/v1/trading/order-cash` | `TTTC0011U` | `VTTC0011U` | uses mock TR | — | `tests/test_kis_order_ops.py` |
| `cancel_korea_order` | cancel-submit | `/uapi/domestic-stock/v1/trading/order-rvsecncl` | `TTTC0013U` | `VTTC0013U` | uses mock TR | — | `tests/test_kis_order_ops.py` |
| `modify_korea_order` | modify-submit | `/uapi/domestic-stock/v1/trading/order-rvsecncl` | `TTTC0013U` | `VTTC0013U` | uses mock TR | — | `tests/test_kis_order_ops.py` |

---

## US Overseas Endpoints

| endpoint | lifecycle_stage | kis_url | tr_live | tr_mock | is_mock=True behavior | surface error tag | pinned_by_test |
|---|---|---|---|---|---|---|---|
| `inquire_overseas_balance` | holdings/balance | `/uapi/overseas-stock/v1/trading/inquire-balance` | `TTTS3012R` | `VTTS3012R` | uses mock TR | — | `tests/test_kis_account_fetch_stocks.py` |
| `inquire_overseas_margin` | cash/orderable | `/uapi/overseas-stock/v1/trading/foreign-margin` | `TTTC2101R` | **mock_unsupported** (defined as `VTTS2101R`; operator-flagged unreliable on mock account) | skips silently with structured `mock_unsupported` error | `errors[].mock_unsupported=true, market="us"` | `tests/test_portfolio_cash_kis_mock.py::test_cash_balance_mock_uses_domestic_cash_not_integrated_margin` |
| `inquire_overseas_buyable_amount` | cash/orderable | `/uapi/overseas-stock/v1/trading/inquire-psamount` | `TTTS3007R` | `VTTS3007R` (defined; not currently called — record as "defined / unused") | N/A (not called) | — | — |
| `inquire_overseas_orders` | pending-inquiry, cancel/modify-lookup | `/uapi/overseas-stock/v1/trading/inquire-nccs` | `TTTS3018R` | **mock_unsupported** | fails closed (raises `RuntimeError` with `"mock"` in message) | `errors[].mock_unsupported=true` | `tests/test_kis_overseas_pending_mock.py::test_inquire_overseas_orders_mock_fails_closed` |
| `inquire_daily_order_overseas` | daily-history | `/uapi/overseas-stock/v1/trading/inquire-ccnl` | `TTTS3035R` | `VTTS3035R` | uses mock TR | — | `tests/test_kis_mock_routing.py::test_inquire_daily_order_mock_uses_mock_tr[overseas]` |
| `order_overseas_stock` (buy) | order-submit | `/uapi/overseas-stock/v1/trading/order` | `TTTT1002U` | `VTTT1002U` | uses mock TR | — | `tests/test_kis_order_ops.py` |
| `order_overseas_stock` (sell) | order-submit | `/uapi/overseas-stock/v1/trading/order` | `TTTT1006U` | `VTTT1006U` | uses mock TR | — | `tests/test_kis_order_ops.py` |
| `cancel_overseas_order` | cancel-submit | `/uapi/overseas-stock/v1/trading/order-rvsecncl` | `TTTT1004U` | `VTTT1004U` | uses mock TR | — | `tests/test_kis_order_ops.py` |
| `modify_overseas_order` | modify-submit | `/uapi/overseas-stock/v1/trading/order-rvsecncl` | `TTTT1004U` | `VTTT1004U` | uses mock TR | — | `tests/test_kis_order_ops.py` |

---

## Mock-Unsupported TR Summary

The following live TRs have **no verified mock equivalent** and are treated as
`mock_unsupported`. The frozenset in
`tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented` pins
these values as a regression guard.

| TR ID | Endpoint | Reason |
|---|---|---|
| `TTTC8036R` | `inquire_korea_orders` | KIS docs claim "실전/모의 공통" but mock account returns `EGW02006 모의투자 TR 이 아닙니다`. Fail-closed added in ROB-31. |
| `TTTS3018R` | `inquire_overseas_orders` | No mock TR published by KIS. Fail-closed added in ROB-28. |
| `TTTC0869R` | `inquire_integrated_margin` | No mock TR (`VTTC0869R` is defined but not confirmed working). Fail-closed added in ROB-28. |
| `TTTC2101R` | `inquire_overseas_margin` | Mock TR `VTTS2101R` is defined but operator-flagged as unreliable on mock account. Surface treats as `mock_unsupported`. |

---

## How to Add a New Lifecycle Endpoint

When wiring a new KIS endpoint that has a mock TR:

1. Add the `_TR_MOCK` constant to `app/services/brokers/kis/constants.py`.
2. Implement `if is_mock: tr_id = constants.FOO_TR_MOCK else: tr_id = constants.FOO_TR` in the broker method.
3. Add a row to this matrix with `pinned_by_test` pointing to a new test.
4. Update `app/mcp_server/README.md` if the endpoint is exposed via MCP.

When wiring a new KIS endpoint that **lacks** a mock TR:

1. Add `if is_mock: raise RuntimeError("... not available in mock mode.")` at the top of the broker method.
2. Add `mock_unsupported` to `tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented`.
3. Add a row to this matrix with `tr_mock = mock_unsupported`.
4. Add a test asserting `RuntimeError` with `"mock"` in message (mirror of `tests/test_kis_domestic_pending_mock.py`).
5. Update `app/mcp_server/README.md` "KIS mock unsupported endpoints".

When a previously unsupported TR gains a working mock equivalent:

1. Remove the `if is_mock: raise RuntimeError(...)` guard from the broker.
2. Remove the TR from `tests/test_kis_constants.py::test_mock_unsupported_tr_set_is_documented`.
3. Update this matrix (`tr_mock` column and `is_mock=True behavior`).
4. Update `app/mcp_server/README.md`.
