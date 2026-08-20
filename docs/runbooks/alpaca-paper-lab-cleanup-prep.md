# Alpaca paper lab cleanup — corrected preparation record and contract draft

Status: T2 preparation only; not authorization or execution. R2 made no broker API call or broker mutation.

## 1. Evidence and bounded-snapshot limits

The 2026-08-20 bounded epoch (14:45:56.442963–14:45:57.485594 UTC) read the lab account (`efd5…e6cd` masked): UBER qty=1, qty_available=1, average entry price=69.65, and open orders=0. It successfully read account, cash, all positions, open orders (limit=500), and recent ledger (limit=200); neither bound was reached.

The original raw broker responses were not retained, so the previously claimed snapshot digest `54b30c51ff59b2dc3136588ff2461112613137c1f79db825f20dac4cc893a290` cannot be recomputed. It is retired as non-reproducible evidence and cannot bind any execution. The normalized, reviewable reconstruction is [alpaca_lab_cleanup_prep_evidence_r2.json](../../../herdr-inbox/jobs/alpaca-lab-cleanup-prep-20260820-2340/events/alpaca_lab_cleanup_prep_evidence_r2.json), SHA-256 `948f2beb27ac974ab7a47323cf36897b0e13cfe6d796c98d7bfcd4fe30240493`. It is not a fresh broker snapshot. `operator_authorization=null`.

R2 counter: submit=0, cancel=0, modify=0, close_or_liquidate=0, total=0. No code or configuration changed.

## 2. Corrected residual attribution and enumeration boundaries

### UBER is ledger-linked, but not B0-X-lane linked

| Ledger id | Client order id | State | Broker order id | UBER buy evidence (KST) |
| --- | --- | --- | --- | --- |
| 64 | `dlab-rob73-38ca9124b2b93def` | canceled | `1ea64491-65ef-4792-9e22-2795d617bbcc` | requested 1 @50.00; filled 0; created 2026-07-28 22:43:47; canceled 22:43:56 |
| 66 | `dlab-rob73-9c09364239a8e275` | filled | `bd2694bf-bf80-4445-a5b5-3f84bf63a8e6` | requested 1 @69.70; filled 1.00000000 @69.65000000; created 2026-07-28 23:23:17 |

The filled row matches the bounded broker position on `account_mode=alpaca_paper_lab`, symbol UBER, quantity 1, average entry price 69.65, and broker order ID. Ledger net long is `0 + 1 = 1`, exactly the broker quantity. `derive_client_order_id` in `app/services/alpaca_paper_submit_service.py` emits `dlab-rob73-…` for the lab account. Thus this is **ledger-linked / B0-X-lane-unattributed**, not foreign or generally unattributed.

The original reader intentionally filtered to `b0xu-` correlations before `_attribute_positions`, which incorrectly promoted a known `dlab-` lab row to “not ours.” The follow-up attribution repair accepts `b0xu-` and `dlab-` execution evidence for **positions** only. Open-order ownership remains `b0xu-` only, preserving the B0-X cancellation boundary; `dlab-` does not become a B0-X pending order.

This correction does **not** make UBER generally tradeable: the canceled row has no fill price, so cumulative deployment remains unreadable and blocks additional buys; its two execution events also fail the single-native-buy requirement for an automated sell source.

The recorded cycle `/Users/mgh3326/work/herdr-artifacts/b0x/alpaca_paper_lab/20260814T224344-us-cycle.json` has the same `foreign_position_symbols=["UBER"]` / no-`b0xu-` evidence and `submission_skipped="contaminated lab account state — foreign/unlinked residue blocks submit"`. The stated skip cause is therefore confirmed for that recorded cycle. It proves an attribution-scope defect in the decision input, not that liquidation is necessary.

### Enumeration source matrix

| Source | Status | Result / limitation |
| --- | --- | --- |
| Current broker account, positions, open orders | prior bounded epoch | UBER 1; open orders 0 |
| Recent internal Alpaca ledger | now enumerated | two rows above |
| Broker closed/filled/canceled history | **not queried** | not enumerated; no broker call is allowed in R2, so no account-wide history completeness claim |
| `operator_contract.yaml` exception | now enumerated | `alpaca_account_cleanup_20260805`: lab suffix `a9e6cd`, UBER qty=1 |

The YAML suffix agrees with the masked snapshot suffix. Its exception is one-shot `account_cleanup`, requires client order ID and ledger record, excludes scoring, forbids scope expansion/reuse after execution, and has consumption status **undetermined**. `mock/CLAUDE.md` §1 identifies the YAML as machine-readable authority.

### Baseline debt

The known focused failure is `test_alpaca_paper_orders_tools_not_registered_default_profile`. Its cause is `ALPACA_PAPER_DEFAULT_TOOLS_ENABLED=true`, mapping to `settings.alpaca_paper_default_tools_enabled`, which registers the three Alpaca mutation/ledger tools in DEFAULT. Without that setting it passes; CI has no prod env and is unaffected. This is environment-sensitive baseline debt, not a PR regression; no gate changed. The other two upstream debt selectors remain unknown.

## 3. Writer and prior-cleanup review

Manual `alpaca_paper_submit_order`/`alpaca_paper_cancel_order` accepts the lab account but consumes no seal and does not reject unsealed cleanup targets. B0-X submit/cancel defaults are unwired (`LabMutationNotWired`). No existing writer satisfies **this seal-bound cleanup contract**.

The `cleanup-btc-rob85-20260505` and `cleanup-sol-rob85-20260505` ledger precedents reached `final_reconciled` using explicit client IDs and ledger records. They show a historical pattern, not present authority or a compliant writer. Two T3 implementation prerequisites are explicit: a reviewed seal-bound writer and an account-scoped cleanup lease/freeze; neither exists.

The D2 statement is scoped: `d2_remediation_single` was not found **in this auto_trader repository**. The adjacent operator contract names it for Binance remediation, not for Alpaca; no assertion is made about implementation outside this repository.

## 4. Existing-exception comparison — operator decision required

No choice is made here.

| Option | Supporting evidence | Required before T3 |
| --- | --- | --- |
| A. Existing exception sufficient | It already names exact lab suffix and `(UBER, qty=1)`, with one-shot client-ID and ledger requirements. | Prove unconsumed; bind current action/qty/price to a fresh snapshot; provide reviewed writer and lease/freeze. YAML has no current side, price, seal hash, or writer. |
| B. New exception required | Required if prior one-shot was consumed/ambiguous or the action differs from exact account/symbol/quantity scope. | Operator must define a new narrow bound action; no expansion may be inferred from the old entry. |

Two evidence-based paths remain unselected: (1) recognition—repair account-wide attribution consumption so known `dlab-` legacy inventory does not become B0-X contamination, potentially removing the need for liquidation; (2) cleanup—only after the operator resolves A/B and all T3 preconditions, execute the exactly authorized action from a fresh snapshot.

## 5. Draft T3 contract (not authorized)

If the operator selects cleanup: require fresh bounded snapshot and authorization; implemented account lease/freeze; open-order-first read; strict binding of account, symbol, action, qty, price, broker ID and current artifact hash; one request per binding/no blind retry; broker readback plus second account-truth proof; and release only after proof or a recorded anomaly. Any unsealed/extra target or failed read remains fail-closed.
