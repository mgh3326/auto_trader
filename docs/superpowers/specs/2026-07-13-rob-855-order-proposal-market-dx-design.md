# ROB-855 Order Proposal Market DX Design

## Goal

Make `order_proposal_create` accept the operator-facing market aliases used by
the rest of the MCP surface while preserving canonical order-proposal storage,
hashing, validation, and responses. Improve validation guidance for proposal
contracts and trade-retrospective outcomes.

## Scope

- Accept `kr`, `us`, and `crypto` at the `order_proposal_create` MCP boundary.
- Normalize them to `equity_kr`, `equity_us`, and `crypto`, respectively.
- Pass only the normalized value to target-order preflight and
  `OrderProposalsService.create_proposal`.
- Keep the service contract and persisted order-proposal market values
  canonical.
- Enumerate supported account-mode/market combinations and market aliases in
  proposal contract errors.
- Document the create tool's market values and account-mode combinations.
- Enumerate all actual retrospective outcomes in validation errors and the
  `save_trade_retrospective` docstring.
- Do not modify `app/services/order_proposals/revalidation.py`, including the
  Toss submit path.
- Do not change `no_resolvable_forecast` auto-close behavior.

`order_proposal_get` accepts only a proposal ID and `order_proposal_list`
currently filters only by symbol and lifecycle state. Neither has a market
filter to normalize, so this change does not add a new public parameter.

## Architecture and Data Flow

Add one focused market-normalization helper in
`app/mcp_server/tooling/order_proposal_tools.py`. Call it at the start of
`order_proposal_create`, before replace/cancel target-order preflight and before
opening the service session.

The resulting flow is:

1. MCP receives `kr`, `us`, `crypto`, or a canonical market string.
2. The tool normalizes `kr` to `equity_kr` and `us` to `equity_us`; other
   strings pass through unchanged so the service remains the source of truth
   for contract rejection.
3. Target-order preflight, when applicable, receives the canonical market.
4. `OrderProposalsService.create_proposal` validates the canonical tuple.
5. The service computes `payload_hash` from the canonical market.
6. The repository stores the canonical market, and read responses serialize
   that stored value.

Keeping normalization at this single boundary ensures alias and canonical
inputs produce identical persisted payload hashes without changing the generic
hash function's contract.

## Validation and Documentation

Proposal contract rejection will retain the rejected tuple and append:

`allowed: kis_live×equity_kr|equity_us, toss_live×equity_kr|equity_us, upbit×crypto; market aliases kr→equity_kr, us→equity_us`

The `order_proposal_create` docstring will describe:

- canonical markets: `equity_kr`, `equity_us`, `crypto`;
- aliases: `kr`, `us`;
- supported place combinations: KIS live and Toss live with either equity
  market, and Upbit with crypto.

The retrospective service's actual outcome set is `filled`,
`partially_filled`, `unfilled`, `rejected`, and `cancelled`. Invalid-outcome
errors and the `save_trade_retrospective` docstring will enumerate all five.

## Persistence and Migration

No migration is required. The order-proposal table's market check constraint
does not admit `kr` or `us`, and the existing service submit-contract allowlist
admits order proposals only under canonical `equity_kr`, `equity_us`, or
`crypto` values. Existing valid rows therefore remain canonical; aliases are
accepted only transiently at the MCP boundary.

## Tests

Use TDD and add regression coverage proving:

1. `market="kr"` creates successfully and the fetched persisted proposal has
   `market="equity_kr"`.
2. `market="jp"` is rejected with the allowed combinations and alias guidance.
3. Equivalent alias and canonical create inputs persist the same
   `payload_hash`.
4. An invalid retrospective outcome error includes every actual outcome value.
5. Tool documentation exposes the market/account combinations and retrospective
   outcomes.

Run the focused red/green tests during implementation, then run:

```bash
uv run pytest tests/services/order_proposals/ tests/mcp_server/ -q -k "proposal or retrospective"
make lint
```

No test may make a real broker call.
