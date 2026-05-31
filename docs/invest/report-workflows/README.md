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

Do not commit:

- secrets, bearer tokens, cookies, browser profile paths, or MCP authorization
  values;
- generated private reports containing account balances, holdings, or realized
  PnL;
- local `.mcp.json` files or operator-specific routing;
- live order, watch, order-intent, scheduler, or broker-mutation instructions.

These docs are safe to keep in the public repository because they describe
contracts, stage vocabulary, and sanitized prompt structure only.
