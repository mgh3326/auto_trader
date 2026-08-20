# Alpaca paper lab cleanup — preparation record and execution-contract draft

Status: preparation only; it is not an authorization or an execution plan.

## 1. Sealed account-truth snapshot

Artifact: `alpaca_lab_cleanup_prep_snapshot.v1`.

| Field | Value |
| --- | --- |
| Account mode | `alpaca_paper_lab` |
| Epoch (UTC) | 2026-08-20T14:45:56.442963+00:00 to 2026-08-20T14:45:57.485594+00:00 (1042.631 ms) |
| Account identity | `efd5…e6cd` (masked) |
| Account status / cash / buying power / NAV | ACTIVE / 99930.34 / 399941.37 / 100008.92 |
| Positions | UBER: qty=1, qty_available=1, avg_entry_price=69.65 |
| Open orders | 0 |
| Read result | all five reads succeeded; no retry; neither 500-order nor 200-ledger bound was reached |
| Broker mutation calls | submit=0, cancel=0, modify=0, close_or_liquidate=0, total=0 |

The artifact read, in one bounded concurrent epoch: `alpaca_paper_get_account`,
`alpaca_paper_get_cash`, `alpaca_paper_list_positions`,
`alpaca_paper_list_orders(status="open", limit=500)`, and
`alpaca_paper_ledger_list_recent(limit=200)`.  It stores SHA-256 of each raw
response, not raw broker payloads, and SHA-256 of canonical JSON with
`seal.artifact_sha256` omitted from the preimage.  The five response digests,
in that order, are `c870ebab90f1259dd0800c7e858b11d59c7256e46d38b323091985b6a1455867`,
`9e053064c78d0282f13e6f2993726e6e42462dc7223445ce2e3e0324883187e4`,
`fe4653da589ad27d696e354f98ec0593c197efbd5b9102ac6fb8483b89f2a0f1`,
`9ffd355381d628ef5ad958729bd73ea62a5a3ecfec270ef2ae117c2b4b68b3b7`, and
`1b99bf5f9a6a6d066331d1b158e99c0b4a52fabb09c807ac43a40005eec2923b`.
Its artifact SHA-256 is
`54b30c51ff59b2dc3136588ff2461112613137c1f79db825f20dac4cc893a290`.

`operator_authorization` is `null`.  This hash only identifies this
observation artifact; it is not an operator signature, approval, or permission
to mutate the broker account.

## 2. Residual attribution ledger

| Residual | Quantity / state | Attribution | Evidence | Disposition |
| --- | --- | --- | --- | --- |
| Position UBER | qty=1; qty_available=1 | **unattributed** | The bounded recent ledger contained 2 rows and zero `b0xu-` execution rows. `scripts/b0x/us/alpaca.py::_attribute_positions` therefore returned `UBER: no b0xu execution correlation`. | Blocker. It is not permission to sell. |

There were no open-order residuals.  The classification method is exactly the
implemented B0-X lab reader: `_b0xu_executions`, `_classify_open_orders`, and
`_attribute_positions` in `scripts/b0x/us/alpaca.py` (SHA-256
`52783f231ce05ac077d901f0dee0b65ae19b25bc6be69bdf3ea8079c2779cb75`).  A
position is linked only when the signed quantity from evidence-bearing
`b0xu-` fills exactly equals the broker quantity; a broker order is linked only
when exactly one such lifecycle correlation names its broker order ID.  No
prefix, account name, or plausibility inference was used.

### Baseline debt (observation only)

Upstream ROB-1303 adversarial verification reported three default-profile
Alpaca failures on `origin/main`.  This preparation round did not alter that
baseline.  A focused read-only test collection directly reproduced one of the
three under the supplied production environment: 57 passed and
`tests/test_alpaca_paper_orders_tools.py::test_alpaca_paper_orders_tools_not_registered_default_profile`
failed because `ALPACA_PAPER_MUTATING_TOOL_NAMES` intersected the registered
default-profile tools.  The upstream verifier artifact did not supply the
other two selectors, so they remain explicitly unresolved rather than guessed:

| Debt ID | Attribution | Evidence | State |
| --- | --- | --- | --- |
| `ROB-1303-default-profile-alpaca-01` | default-profile registration environment | focused collection reproduced `tests/test_alpaca_paper_orders_tools.py::test_alpaca_paper_orders_tools_not_registered_default_profile`: mutation-tool set was not disjoint from default registered tools | baseline debt; no fix in this round |
| `ROB-1303-default-profile-alpaca-02` | unknown | same upstream report | baseline debt; NEEDS_VERIFY |
| `ROB-1303-default-profile-alpaca-03` | unknown | same upstream report | baseline debt; NEEDS_VERIFY |

## 3. Writer existence review

The only implemented lab-account broker mutation surfaces found were reviewed
without calling them:

| Surface | Exists | Why it is not named as cleanup writer |
| --- | --- | --- |
| `alpaca_paper_submit_order` / `alpaca_paper_cancel_order` in `app/mcp_server/tooling/alpaca_paper_orders.py` | yes; accepts `account_mode="alpaca_paper_lab"` and requires `confirm=True` for broker mutation | The single-order manual path does not consume this seal or enforce a sealed cleanup target set. |
| `scripts/b0x/us/alpaca.py::submit_planned_order` / `cancel_own_open_orders` | code exists | Both raise `LabMutationNotWired` without an injected callback; the B0-X runbook §4 says the default production mutation seam is intentionally unwired. |
| `AlpacaPaperOrderApplication` | yes | It is default-`alpaca_paper`/crypto application plumbing, not a lab cleanup writer bound to this artifact. |

Therefore **no existing writer satisfies this cleanup contract**.  The later
execution round has a hard precondition: implement and independently review a
lab-only cleanup writer that consumes the current seal and rejects any target
outside it.  It must use `AlpacaPaperLedgerService` for ledger writes; direct
ledger SQL is not allowed.  This conclusion is based on
`scripts/b0x/us/alpaca.py`, `scripts/b0x/us/cycle.py`,
`app/mcp_server/tooling/alpaca_paper_orders.py`,
`app/services/alpaca_paper_submit_service.py`, and
`docs/runbooks/b0x-us-cycle.md` (respective SHA-256 values recorded in the
implementation report).

## 4. Draft cleanup execution contract — not authorized

This is a draft for the separate T3 execution round only.

1. Preconditions: an operator separately authorizes the round; a reviewed
   writer from §3 exists; the source snapshot is re-read and re-sealed; and the
   new seal has `operator_authorization` populated only by that separate
   authority.  The UBER residual remains blocked until its ownership is proven
   or an operator resolves it explicitly; unattributed quantity is never an
   implicit sell authorization.
2. Freshness: execution must reject an expired seal.  It must re-seal and
   obtain fresh authorization when the configured TTL elapses or the current
   position/open-order truth or price/evidence differs from the sealed values.
   This prevents a stale-price execution such as the previously observed
   +7.41% divergence.
3. Freeze and lease: before any broker mutation, acquire an account-scoped
   cleanup lease/freeze and record the immutable artifact hash.  Failure to
   acquire it is fail-closed.
4. Open orders first: read all open orders under the lease.  Any order absent
   from the sealed set, any read failure, or any attribution ambiguity blocks
   the entire execution.  The writer may only address a sealed, explicitly
   authorized order ID.
5. Binding: each action must bind `(account_mode, symbol, action, quantity,
   limit/reference price, source broker order ID when applicable, artifact
   SHA-256)`.  The writer must reject extra, substituted, partial, or
   unsealed targets.  It must never broaden the symbol set from a fresh read.
6. Execution and retry: issue at most one broker request for each binding.
   On timeout, transport failure, or ambiguous response, do **not** blind
   retry.  Perform bounded readback by broker order ID/client order ID and
   record the observed state; unresolved evidence remains a blocker.
7. Double proof: terminal success requires broker readback plus a second
   account truth read proving the expected open-order/position delta.  Ledger
   state alone is insufficient; a fill is marked only from broker evidence.
8. Restore: release the freeze/lease only after the double proof or an
   explicitly recorded unresolved anomaly.  A failed or ambiguous execution
   leaves the account frozen for operator disposition.

The draft's verified repository anchors are the B0-X runbook §4 (unwired lab
mutation seam), `scripts/b0x/us/alpaca.py::read_fresh_truth` (bounded read and
attribution rules), and `app/mcp_server/tooling/alpaca_paper_orders.py`
(per-call confirmation and cancel readback).  No repository source containing
the cited D2 contract text or a `d2_remediation_single` writer was found;
those D2 clause numbers and enforcement locations are therefore
**unconfirmed**, not asserted here.
