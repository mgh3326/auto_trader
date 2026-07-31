# Strategy ownership manifest (ROB-1189)

`research_contracts.strategy_ownership_manifest` is a stdlib-only, immutable
static contract. It exposes a JSON-ready manifest and a deterministic validator;
it performs no broker, database, network, ledger, environment, or runtime load.

Each truth-bearing field is an `EvidenceFact` with a source locator, evidence
status, and (when not accepted) a reason. `MISSING`, `DRAFT`, `STALE`, and
`CONFLICT` always serialize as `UNKNOWN`; they never become false, zero, empty,
active, or a physical-account identity.

The contract separates strategy experiments, portfolio lanes, logical account
surfaces, physical broker-account identities, designated writers, current broker
observations, and local ledger lifecycle. A mode, keyset, tool, env gate, code
path, or ledger cannot prove physical-account identity or enable a writer.

The fixed slots are one DFC-4H public-data shadow/no-order flagship, one AP-A1/
AP-A2 offline challenger, one KR-B1 KRX·Koscom external-evidence unblocker, and
zero new hypothesis-family admissions. The remaining allowed categories are
quarantine, infrastructure, and reserve.

Run the contract test with:

```bash
uv run pytest tests/research_contracts/test_strategy_ownership_manifest.py -q -ra
```
