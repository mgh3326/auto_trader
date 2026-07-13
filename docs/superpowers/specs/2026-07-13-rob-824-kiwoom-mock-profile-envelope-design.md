# ROB-824 Kiwoom Mock Profile Envelope Design

## Goal

Expose only the existing typed Kiwoom mock tools needed by the
`account_read` and `tradingcodex_execution` profiles, and add stable normalized
position/order envelopes without weakening the mock-only execution boundary.

## Scope

- `account_read` adds exactly these three read tools:
  `kiwoom_mock_get_positions`, `kiwoom_mock_get_orderable_cash`, and
  `kiwoom_mock_get_order_history`.
- `tradingcodex_execution` adds exactly the existing seven
  `kiwoom_mock_*` tools.
- No new profile is created. `default` is not expanded beyond the existing
  ROB-601 flag gate.
- Kiwoom lifecycle, reconciliation, fills, and P&L remain ROB-852 work.

## Registration

The existing `orders_kiwoom_variants.register()` function remains the single
registration source. Each restricted profile wraps its MCP instance with the
existing allowlist filter before invoking that function. The account-read
allowlist admits only the three reads, while the execution allowlist admits the
complete typed set. Forbidden-set construction subtracts the newly allowed
names from `KIWOOM_MOCK_TOOL_NAMES`, so an accidental future Kiwoom tool is
forbidden by default.

The typed tools register even while `KIWOOM_MOCK_ENABLED` is false, matching the
isolated Kiwoom profile: every invocation fails closed through
`validate_kiwoom_mock_config`. Startup remains valid while the feature is off.
When the feature is enabled on either restricted profile, startup validates the
required mock configuration and the exact `https://mockapi.kiwoom.com` host.
This prevents a partially enabled or live-target configuration from presenting
an apparently usable TradingCodex endpoint.

## Stable read envelopes

Broker-specific parsing lives in
`app/services/brokers/kiwoom/normalization.py`. The MCP handlers retain the raw
broker payload as redacted `broker_response` evidence and add:

- `positions`: `symbol`, `quantity`, `average_price`, `currency`
- `orders`: `order_id`, `symbol`, `status`, `ordered_price`,
  `filled_quantity`, `average_price`, `remaining_quantity`
- `provenance`: broker `kiwoom`, environment/account mode `mock` /
  `kiwoom_mock`, fixed host `mockapi.kiwoom.com`, and the expected API ID
  (`kt00018` or `kt00009`)

Official response keys are the parser contract:

- kt00018: `acnt_evlt_remn_indv_tot[].stk_cd`, `rmnd_qty`, `pur_pric`
- kt00009: `acnt_ord_cntr_prst_array[].ord_no`, `stk_cd`, `ord_qty`,
  `ord_uv`, `cntr_qty`, `cntr_uv`, `mdfy_cncl_tp`

Numeric strings are converted to integers after removing commas and Kiwoom
left padding. `A005930` is normalized to `005930`; an unrecognized symbol is
rejected. Order status is `cancelled` when the broker cancellation marker says
so, otherwise derived as `open`, `partially_filled`, or `filled` from ordered
and filled quantities. Missing or malformed required fields fail the whole
normalized call rather than dropping a row.

## Provenance and redaction

The transport remains constructor- and send-boundary pinned to the mock host.
Before normalization, the response is checked for explicit provenance objects
or fields that claim a non-mock account mode, live/production environment, or a
non-mock Kiwoom host. Any conflict returns `success=false` and no normalized
rows.

`broker_response` is a deep copy with authorization/token/app secret/account
identifier values replaced by `[REDACTED]`. Normal non-secret broker evidence,
including return codes, continuation keys, position rows, and order rows,
remains available.

## Tests

1. Exact profile and forbidden matrices prove account-read has three Kiwoom
   reads and zero mutations, while execution has exactly seven typed Kiwoom
   tools and no Kiwoom live/general unscoped surface.
2. Startup tests prove off-state startup succeeds, enabled incomplete config
   fails with names only, and a live/non-mock base URL fails.
3. Normalizer/handler tests prove official kt00018/kt00009 shapes, empty arrays,
   malformed-row fail-close, redaction, and conflicting live provenance.
4. Existing config, endpoint, KRX, dry-run/confirm, profile, registry, and MCP
   startup tests remain green.

## Self-review

- No placeholders or deferred implementation details remain.
- The profile surfaces, parser keys, and envelope names match the Linear issue
  and user request.
- No DEFAULT expansion, new profile, live Kiwoom path, lifecycle, reconcile, or
  P&L behavior is included.
