# /invest report workflow contracts

This directory contains public-safe contracts and prompt templates for staged
`/invest/reports` generation. These documents describe what the application
contract should guarantee; private operator workspaces may wrap these contracts
with local MCP routing, credentials, browser sessions, and generated reports.

## Documents

- [`kr-staged-report-workflow.md`](kr-staged-report-workflow.md) — KR market
  staged report flow for pre-market, NXT open, regular open, and later intraday
  extensions.
- [`kr-report-composer-template.md`](kr-report-composer-template.md) — sanitized
  Hermes composer prompt template that can be copied into private operator
  prompts without committing private account state or credentials.
- [`candidate-lineage-contract.md`](candidate-lineage-contract.md) — candidate
  status, transition, and evidence-lineage vocabulary used across stages.

## Boundary

`auto_trader` owns deterministic data preparation, bundle freezing, context
export, ingest, persistence, and UI contracts. Hermes or an external agent owns
LLM reasoning and composition. The application must not import or call an LLM
provider in-process to compose investment advice.

Canonical flow:

```text
investment_report_prepare_bundle
  -> investment_report_get_hermes_context
  -> Hermes compose stage artifacts + final composition
  -> investment_stage_artifacts_ingest_from_hermes
  -> investment_report_create_from_hermes_composition
```

Direct read-only MCP calls are useful for diagnosis and gap discovery. Once a
piece of direct-tool evidence is repeatedly required for a report, it should be
promoted into a durable read model and a snapshot collector so it can be frozen
inside a bundle.

## Public-safety rules

### Procedure contract vs. operator instruction

Two kinds of "instruction" are easy to conflate; only one is safe to commit.

- **Procedure contract (allowed):** the _shape_ of a decision flow — which tools
  run in which order, which gates apply, which policy keys govern thresholds.
  A procedure contract is generic: it names tools, lanes, and policy keys, never
  a specific account, balance, or credential. Example:
  [`docs/playbooks/`](../../playbooks/README.md).
- **Operator instruction (forbidden):** anything that binds the procedure to a
  specific operator's account state or secrets — account numbers, balances,
  asset size, holdings, order amounts tied to a real account, credentials, or
  routing.

A document that says "the buy lane calls `toss_place_order` after the recovery
gate passes" is a procedure contract and may be committed. A document that says
"buy ₩X of symbol Y in account NNNN" is an operator instruction and must not be.

Do not commit:

- secrets, bearer tokens, cookies, browser profile paths, or MCP authorization
  values;
- generated private reports containing account balances, holdings, or realized
  PnL;
- account numbers, per-account order amounts, or total asset size;
- local `.mcp.json` files or operator-specific routing;
- live order, watch, order-intent, scheduler, or broker-mutation instructions
  **bound to a specific account** (the generic procedure that _describes_ such a
  lane is a procedure contract and is allowed).

These docs are safe to keep in the public repository because they describe
contracts, stage vocabulary, sanitized prompt structure, and generic procedure
flows only.
