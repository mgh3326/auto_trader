# ROB-67 — `paper_001` / `모의투자1` mock trading instance

`paper_001` is the first broker-agnostic mock trading instance for the KIS
official mock runtime smoke path.

## Registry contract

The source-of-truth registry is `app/services/mock_trading_instance_registry.py`.
The initial entry is:

| Field | Value |
| --- | --- |
| `slug` | `paper_001` |
| `display_name` | `모의투자1` |
| `broker_backend` | `kis_mock` |
| `broker_account_ref` | `env:KIS_MOCK_ACCOUNT_NO` |
| `market_scope` | `kr` |
| `strategy_profile` | `balanced_kr_mock` |
| `persona_profile` | `paper_001` |

`broker_account_ref` is an environment/config key reference only. Do not commit
real account numbers, KIS app keys, tokens, or secrets in this registry or in
operator reports.

## Safety rules

- `paper_001` must resolve to `kis_mock` only.
- Unknown instance lookup must fail closed.
- The registry is metadata-only; it must not place orders, instantiate broker
  clients, or read credentials.
- Live backend routing, Alpaca paper routing, and daily autonomous trading loops
  are out of scope for ROB-67.

## Follow-up smoke

After ROB-67 is merged and deployed, rerun ROB-66 from the top:

1. Confirm `paper_001` resolves from the registry above.
2. Confirm the dedicated paper MCP runtime still exposes only `kis_mock_*` order
   tools and does not expose generic/live-capable order tools.
3. Only then run KIS mock read-only smoke checks.
4. Run mock order preview/dry-run only if it is explicitly paper/mock-only and
   never with `dry_run=false`.
