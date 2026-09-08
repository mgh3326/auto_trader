# NHPLUG Mock Mirror Orders

Stage 2 is a dedicated counterfactual mirror lane only (`account_mode=nh_mock`).
It never selects an operating account: every dispatch is pinned to
`https://moapi.nhplug.com:8443`, verifies resolved scheme/host/port with
redirects disabled, and rechecks the discovered `acct_type=03` allowlist at
send time. OAuth's documented token/revoke exception remains the only use of
the production OAuth host.

Both gates must be explicitly armed; neither is true by default:

```bash
NHPLUG_MOCK_ENABLED=true
NHPLUG_MOCK_ORDERS_ENABLED=true
```

Required credentials are `NHPLUG_APP_KEY`, `NHPLUG_APP_SECRET`, and
`NHPLUG_MOCK_ACCOUNT_NO`; keep them only in the operator-created minimal env
file. Do not use `01`/`02`, override host variables, or an operating env file.

MCP is available in the default profile when the read gate is enabled and in
`hermes-paper-kis`. The only mutation is `nh_mock_place_order`: it is KRX-only,
defaults to `dry_run=true`, requires `confirm=true` for send, and requires a
real `mirror_cohort="mock_counterfactual"`, strategy/correlation binding, and
the original real-order ledger UUID (`counterfactual_of`). Missing attribution
returns `attribution_required` before account discovery or broker send. Sell
requests must pass the loss-sell guard; a non-zero broker code is always a
failure, never an accepted ledger row.

`review.nh_mock_signal_ledger` commits the signal before send. Accepted sends
only are written through `NHMockOrderLedgerService` to
`review.nh_mock_order_ledger`; fills are confirmed only from
`dailyOrderExecution` reconciliation evidence.

Operator smoke (do not run from CI):

```bash
uv run python -m scripts.nhplug_mock_smoke --mode order-test --symbol 005930 --price 1
NHPLUG_MOCK_ENABLED=true NHPLUG_MOCK_ORDERS_ENABLED=true \
  uv run python -m scripts.nhplug_mock_smoke --mode full --confirm-read --confirm \
  --symbol 005930 --quantity 1 --price <non_marketable_krx_limit>
```

`full` cannot submit unless cancellation is wired and always attempts cancel in
`finally`, then reads `dailyOrderExecution`. If a broker order id is unreadable
or cancellation/reconciliation fails, stop, preserve the safe output, and use
the NH mock UI/approved cancel tool for cleanup; never re-send to recover.
