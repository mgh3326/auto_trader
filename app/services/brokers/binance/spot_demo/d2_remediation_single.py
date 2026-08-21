"""D2 one-shot remediation writer — ``d2_remediation_single``.

``operator_contract.yaml`` names this writer by hand::

    binance_demo_remediation_20260820:
      surfaces:      [binance_spot_demo_remediation_only]
      writer:        d2_remediation_single
      allowed_operations:
        - BTCUSDT_sell_limit_bound_to_sealed_snapshot
        - ETHUSDT_sell_limit_bound_to_sealed_snapshot
        - USDCUSDT_sell_limit_conversion

Naming a writer does not create one, and until this module existed there was
nothing to run: the D2 execution attempt of 2026-08-20 stopped at
``D2_DUAL_ATTESTATION=FAIL`` because a static search for the named writer
returned no match.  This module is that named writer and nothing else.

**It is a second approved entry point, not a bypass.**
``scripts/binance_spot_demo_smoke.py`` is the ROB-298 BUY-round-trip smoke
path and stays exactly what it is; it does not wire a SELL ``--confirm`` and
therefore cannot express the three authorized operations.  Reaching around it
by calling :meth:`BinanceSpotDemoExecutionClient.submit_order` from an ad-hoc
script would have created an *unreviewed* execution surface.  A reviewed,
narrower one is the correct answer, so this module exists and both CLIs name
each other.

**Exactly one sealed object in this repository authorizes a dispatch, and it
is the r8 execution authority.**  Operator decision §134차 (2026-08-21) reads:

    「§132차가 바인딩한 r8 봉인 — pre_snapshot_hash 4816a1d9…66f830 ·
      manifest fa7092a3…e8431 · 수량/가격 3건 §132차 원문 — 한정으로 dispatch 를
      허용한다. 그 외 봉인은 계속 거부한다.」

"그 외 봉인은 계속 거부한다" — *every other seal stays refused* — is the half
this module has to enforce, so the permission is expressed as a closed set of
one digest (:data:`D2_DISPATCH_AUTHORIZED_DIGESTS`) rather than as a flag a
second entry could also carry.  :func:`_assert_closed_order_set` refuses to
import unless the set of ``dispatch_authorized`` registry entries is *equal* to
that closed set, so marking any other payload authorized is an import failure
rather than a quiet dictionary edit.

The r8 snapshot payload itself is **not** the authority and was not rewritten
to become one: it is registered (digest ``00b975ff…``) with
``dispatch_authorized=False``, exactly as it was sealed — unsigned, no expiry,
``mutation_authorized=false`` on all three rows.  The authority is a separate,
execution-only artifact that *names* the r8 seal by digest
(``docs/contracts/d2-r8-execution-authority-20260821.json``), and
:func:`_assert_execution_authority_shape` re-checks that naming from the file's
own bytes at load time.  A dispatch-authorized payload that binds some other
snapshot, manifest, or seal timestamp is refused even if its digest were
somehow registered.

That is a statement about this module's entry points, not about the process.
See the threat model below.

Every ROB-298 safety boundary is inherited unchanged and none is widened:

* the transport still pins ``demo-api.binance.com`` (this module re-asserts
  the host through :func:`assert_spot_demo_host` before it composes anything);
* ``submit_order(..., confirm=True)`` remains the only route to a signed POST,
  and this module's own default is likewise ``confirm=False``;
* ``BINANCE_SPOT_DEMO_ENABLED`` still gates client construction, and this
  module adds a *second*, independent, default-off gate
  (:data:`D2_REMEDIATION_ENABLED_ENV`) on top of it.  Both are read from the
  real process environment; there is no injectable ``environ`` seam.

What this module adds on top of those inherited gates:

1. **A sealed authority, verified from file bytes.**  Orders are never taken
   as an argument.  :func:`load_sealed_authority` reads a file, hashes the
   bytes, refuses an unknown digest, re-derives the three orders, and requires
   them ``==`` the frozen :data:`D2_BOUND_ORDERS`.  It then evaluates dispatch
   authority separately: ``operator_authorization`` must be non-null, the
   expiry must not have passed, every actionable row must carry
   ``mutation_authorized=true``, and the sealed credential fingerprint must
   match the signed J2A registry.  Each failure is reported by name; none is
   waived.
2. **A typed J3A lease, re-proved per submit.**  The writer requires a real
   :class:`PostgresAdvisoryKeysetLease` — a duck-typed stand-in is rejected by
   ``isinstance`` before anything else runs — and re-proves ownership
   immediately before each submit rather than trusting acquisition time.  The
   ``isinstance`` check catches a wrong object, not a determined one: an
   in-process caller can construct the real class over a fabricated lock
   authority.  See the threat model below.
3. **A durable, deterministic replay fence.**  The ``client_order_id`` is
   derived from the seal and the order, not from a UUID, so the same bound
   order has the same id in every process.  Before any submit the ledger is
   asked whether that id was already attempted; if it was, the writer reads
   the broker instead of sending, and never sends.
4. **A mandatory ledger.**  :class:`BinanceDemoLedgerService` is a required
   constructor argument. There is no ``None`` default and no path that
   dispatches without durable evidence.
5. **Pre-dispatch account truth.**  The account-wide open-order book and the
   three sealed balances are read *before* the first order, not only after the
   last one. A foreign resting order or any balance drift from the seal stops
   the run before a single POST.
6. **Full broker echo.**  Symbol, side, type, quantity, **price**, and
   **timeInForce** are compared by closed equality against the seal; a response
   that cannot prove the sealed price is a failure, not a success.
7. **Two fresh proof epochs**, collected while the lease is still held.

**Threat model (operator-confirmed).**  These gates cover accidental misuse: a
wrong parameter, a doctored or unregistered payload file, an account that has
drifted from the seal, a malformed or foreign broker response, and a re-send
after a crash.  They do **not** cover deliberate forgery by code running in this
same process.  A caller already executing here can read the module-private
authority token, construct a real :class:`PostgresAdvisoryKeysetLease` over a
fabricated lock authority, subclass the ledger service, or rebind any constant
in this module — and, more simply, can call
:class:`BinanceSpotDemoExecutionClient` directly and never enter this file at
all.  No writer-level check closes that, and none here claims to.

Deliberate absences: no scheduler registration, no retry queue, no MARKET
path, no cancel path, no quantity/price arithmetic (the sealed floor values are
used verbatim), and no way to widen the order set at runtime.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoOrderSubmitResult,
    SpotDemoOrderTestResult,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
    SpotDemoDryRunResult,
)
from app.services.brokers.binance.spot_demo.host_allowlist import assert_spot_demo_host
from app.services.mock_integration.coordination import (
    AdvisoryLeaseGrant,
    LockAuthorityConnection,
    PhysicalAccountScope,
    PostgresAdvisoryKeysetLease,
    SqlAlchemyLockAuthority,
    acquire_physical_account_lease,
    physical_account_scope_for_entry,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    get_lane_registry_entry,
)

# --------------------------------------------------------------------------
# Identity — the sealed object this writer is bound to
# --------------------------------------------------------------------------

#: ``operator_contract.yaml`` names this exact string.  Changing it silently
#: detaches the code from the authority that permits it to run.
WRITER_NAME: Final[str] = "d2_remediation_single"

#: ``operator_contract.yaml: strategy_order_exceptions``.
D2_EXCEPTION_ID: Final[str] = "binance-demo-remediation-20260820"

#: ``binding-payload-proposed.json: remediation_id``.
D2_REMEDIATION_ID: Final[str] = "d2-binance-demo-both-remediation-v2.1-20260818"

#: r8 attempt-2 — the only snapshot this writer will act under.  A payload
#: carrying any other hash is refused; there is no override argument.
#: Transcribed verbatim from operator decision §132차, which re-signed the D2
#: binding and supersedes §125차.  Not recomputed here.
D2_PRE_SNAPSHOT_HASH: Final[str] = (
    "sha256:4816a1d93da8a6e754d46b98d01e70aab9a82c7720bd86e0d5720583cc66f830"
)

#: The r7 snapshot this writer used to be bound to.  Kept as a named constant
#: rather than deleted, because :data:`D2_KNOWN_SEALED_PAYLOADS` still registers
#: the r7 payload — parseable, and refused at :func:`bind_sealed_orders` because
#: its ``pre_snapshot_hash`` is no longer the bound one.  A silent drop would
#: turn "superseded" into "unknown digest", which reads like a different fault.
D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH: Final[str] = (
    "sha256:5ba70a814654513c745e45c49206f11cf5f5d061478c668f86602af493e6b898"
)

#: ``r8-reseal-20260821/snapshot/attempt-2/snapshot-seal.json``.  Carried as
#: evidence and re-checked against the execution authority's own binding block.
D2_SNAPSHOT_SEAL_SHA256: Final[str] = (
    "7817e8df2da3f89b03df40caea5e313b3d113fd07328dc4aaad2c4559397d95f"
)

#: The r8 binding payload's exact bytes.  §134차 names it as one of the two
#: values that bound the permission; it is registered below with
#: ``dispatch_authorized=False`` because the payload itself is unsigned and was
#: deliberately **not** rewritten to carry an authorization it never had.
D2_R8_BINDING_PAYLOAD_SHA256: Final[str] = (
    "00b975ff1a291917699588d91154a790765b0f3e7008b141a8f60f6925fbae61"
)

#: ``manifest_sha256`` from §132차 / §134차, verbatim.
D2_R8_ARTIFACT_MANIFEST_SHA256: Final[str] = (
    "fa7092a32e09689927228c400dbc97312af4eb1cd9f76d34a208d1fe7f1e8431"
)

#: ``seal_timestamp_utc`` from §132차, verbatim.
D2_R8_SEAL_TIMESTAMP_UTC: Final[str] = "2026-08-21T01:53:45.145298Z"

#: The execution-only authority artifact introduced under §134차.  It does not
#: restate the r8 snapshot; it names it by digest.  Repository-relative so a
#: reviewer reads the same bytes the runtime hashes.
D2_R8_EXECUTION_AUTHORITY_PATH: Final[str] = (
    "docs/contracts/d2-r8-execution-authority-20260821.json"
)

D2_EXECUTION_AUTHORITY_SCHEMA_VERSION: Final[str] = "d2-execution-authority.v1"
D2_EXECUTION_AUTHORITY_KIND: Final[str] = "D2_EXECUTION_AUTHORITY"
D2_EXECUTION_AUTHORITY_SCOPE: Final[str] = "R8_SEAL_ONLY"

#: The exact bytes of :data:`D2_R8_EXECUTION_AUTHORITY_PATH`.
D2_R8_EXECUTION_AUTHORITY_SHA256: Final[str] = (
    "ba6518e7cafa16160059aafb22cd304d13793e0f8ea7f035d8fbd8ddc967b00b"
)

#: 🔴 The complete set of digests §134차 permits to dispatch — one member.
#: :func:`_assert_closed_order_set` compares the registry's authorized entries
#: against this by ``==``, not by membership, so a *second* authorized digest
#: fails the import just as an *unauthorized* one does.  That is the machine
#: form of "그 외 봉인은 계속 거부한다".
D2_DISPATCH_AUTHORIZED_DIGESTS: Final[frozenset[str]] = frozenset(
    {D2_R8_EXECUTION_AUTHORITY_SHA256}
)

#: The credential fingerprint the signed J2A registry binds to the shared
#: Binance Demo physical account.  Pinned here *and* cross-checked against the
#: registry entry, so a change on either side fails closed rather than silently
#: re-pointing this one-shot at a different account.
D2_CREDENTIAL_FINGERPRINT: Final[str] = (
    "sha256:e33925948f2cb6e03842cca9967b70f11f9242bc5c8f99c69ce0ca5cbc4d73df"
)

#: The J2A lane whose ``physical_account_id`` names the shared Binance Demo
#: account.  Spot, sidecar, and futures all derive the same advisory key, which
#: is exactly why one lease is enough for the whole account.
D2_LANE_ID: Final[str] = "crypto.binance.spot_demo.canonical"

D2_PRODUCT: Final[str] = "spot"
D2_PRODUCT_DOMAIN: Final[str] = "both"
D2_VENUE: Final[str] = "binance"
D2_VENUE_HOST: Final[str] = "demo-api.binance.com"
D2_QUOTE_ASSET: Final[str] = "USDT"

#: Second, independent env gate.  ``BINANCE_SPOT_DEMO_ENABLED`` still has to be
#: true for the client to construct at all; this one is additional and also
#: defaults to off.  Read from ``os.environ`` only — there is deliberately no
#: injectable ``environ`` parameter, because an in-process caller that can pass
#: a dict could otherwise arm a gate the operator left off.
D2_REMEDIATION_ENABLED_ENV: Final[str] = "D2_REMEDIATION_SINGLE_ENABLED"

#: D6 leaves no order-type or time-in-force choice: LIMIT/GTC, explicit
#: quantity, exposure-reducing only.
D2_ORDER_TYPE: Final[str] = "LIMIT"
D2_SIDE: Final[str] = "SELL"
D2_TIME_IN_FORCE: Final[str] = "GTC"

#: Binance client-order-id prefix.  ``d2rem-`` plus a 24-hex digest is 30
#: characters, inside the 36-char constraint the execution client asserts.
D2_CLIENT_ORDER_ID_PREFIX: Final[str] = "d2rem"
_CLIENT_ORDER_ID_DOMAIN: Final[bytes] = b"auto-trader:d2-remediation-single:v1\x00"

#: The sealed payload marks the three actionable rows with this disposition.
#: Dust (SOL/XRP/DOGE) and quote cash (USDT) carry different ones and are
#: therefore never picked up as orders.
_SEALED_ACTIONABLE_DISPOSITION: Final[str] = "REVIEWED_SCOPE_LIMIT_CANDIDATE"

#: Module-private token gating :class:`SealedAuthority` construction. Private
#: by convention, which is all Python offers — see the threat model at the top.
_AUTHORITY_TOKEN: Final[object] = object()


class D2ReasonCode(StrEnum):
    """The complete refusal vocabulary; no free-form alternatives."""

    DISABLED = "d2_remediation_disabled"
    SEAL_UNKNOWN_DIGEST = "d2_seal_unknown_digest"
    SEAL_HASH_MISMATCH = "d2_seal_hash_mismatch"
    SEAL_IDENTITY_MISMATCH = "d2_seal_identity_mismatch"
    SEAL_ORDER_SET_MISMATCH = "d2_seal_order_set_mismatch"
    SEAL_MALFORMED = "d2_seal_malformed"
    SEAL_ACCOUNT_MISMATCH = "d2_seal_account_mismatch"
    DISPATCH_NOT_AUTHORIZED = "d2_dispatch_not_authorized"
    UNAUTHORIZED_OPERATION = "d2_unauthorized_operation"
    LEASE_NOT_HELD = "d2_lease_not_held"
    LEASE_NOT_A_CAPABILITY = "d2_lease_not_a_capability"
    LEASE_SCOPE_MISMATCH = "d2_lease_scope_mismatch"
    LEDGER_REQUIRED = "d2_ledger_required"
    CLAIM_NOT_DURABLE = "d2_claim_not_durable"
    WRITER_FREEZE_VIOLATED = "d2_writer_freeze_violated"
    ACCOUNT_TRUTH_DRIFT = "d2_account_truth_drift"
    PRIOR_ATTEMPT_UNRESOLVED = "d2_prior_attempt_unresolved"
    BLIND_RETRY_REFUSED = "d2_blind_retry_refused"
    BROKER_ECHO_MISMATCH = "d2_broker_echo_mismatch"
    OUTCOME_UNKNOWN = "d2_outcome_unknown"
    HOST_NOT_SPOT_DEMO = "d2_host_not_spot_demo"
    PROOF_EPOCHS_INCOMPLETE = "d2_proof_epochs_incomplete"


class D2RemediationError(RuntimeError):
    """Base class carrying a machine-readable reason code."""

    def __init__(self, reason_code: D2ReasonCode, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}" if detail else str(reason_code))


class D2RemediationDisabled(D2RemediationError):
    """The dedicated env gate is not armed."""


class D2SealBindingMismatch(D2RemediationError):
    """The supplied sealed payload is not the one this writer is bound to."""


class D2DispatchNotAuthorized(D2RemediationError):
    """The seal binds, but nothing in it authorizes a broker mutation."""


class D2UnauthorizedOperation(D2RemediationError):
    """An operation outside the three authorized ones was requested."""


class D2LeaseNotHeld(D2RemediationError):
    """No live, attested J3A lease over the Binance Demo physical account."""


class D2LedgerRequired(D2RemediationError):
    """No durable ledger; the dispatch path is unreachable without one."""


class D2ClaimNotDurable(D2RemediationError):
    """The pre-send claim did not survive its own commit, so nothing may send."""


class D2AccountTruthDrift(D2RemediationError):
    """Live account state does not match what the seal observed."""


class D2PriorAttemptUnresolved(D2RemediationError):
    """This bound order was already attempted and its outcome is not settled."""


class D2BlindRetryRefused(D2RemediationError):
    """A second dispatch of one client_order_id was attempted."""


class D2OutcomeUnknown(D2RemediationError):
    """A dispatch outcome could not be established by readback."""


# --------------------------------------------------------------------------
# The closed order set
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class D2BoundOrder:
    """One of exactly three authorized operations.

    Frozen and slotted so equality is structural: the seal check below is a
    plain ``==`` against :data:`D2_BOUND_ORDERS`, not a name or substring
    match.  ``Decimal`` comparison is by value, so ``0.00015`` and
    ``0.00015000`` are the same order while ``0.00016`` is not.

    ``sealed_free_quantity`` / ``sealed_locked_quantity`` are the balances the
    seal observed.  They are part of the bound identity, not decoration: the
    pre-dispatch truth check compares the live account against them, so a
    payload that keeps the order fields but rewrites the observed balance is a
    different object and is refused.
    """

    symbol: str
    asset: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    time_in_force: str
    sealed_free_quantity: Decimal
    sealed_locked_quantity: Decimal

    def request_params(self) -> dict[str, str]:
        """The exact broker request fields, as strings, for evidence output."""

        return {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.order_type,
            "quantity": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "timeInForce": self.time_in_force,
        }

    @property
    def client_order_id(self) -> str:
        """Deterministic id derived from the seal and this exact order.

        Not a UUID, and that is the whole point.  A UUID makes every process
        believe it is making a first attempt, so a crash after a POST is
        followed by a *new* id and a duplicate order.  Deriving the id means
        the same bound order carries the same id in every process, which lets
        the ledger recognise a prior attempt — and makes Binance itself reject
        the duplicate as a second, independent fence.
        """

        digest = hashlib.sha256(
            _CLIENT_ORDER_ID_DOMAIN
            + b"|".join(
                (
                    D2_PRE_SNAPSHOT_HASH.encode("utf-8"),
                    self.symbol.encode("utf-8"),
                    self.side.encode("utf-8"),
                    self.order_type.encode("utf-8"),
                    format(self.quantity, "f").encode("utf-8"),
                    format(self.price, "f").encode("utf-8"),
                    self.time_in_force.encode("utf-8"),
                )
            )
        ).hexdigest()
        return f"{D2_CLIENT_ORDER_ID_PREFIX}-{digest[:24]}"


#: The three operations, in dispatch order.  🔴 Transcribed **verbatim** from
#: operator decision §132차 — the values were copied, not recomputed and not
#: re-rounded, and they are the same values r8 attempt-2's
#: ``binding-payload-proposed.json`` carries:
#:
#:   BTCUSDT  SELL LIMIT 0.00015000    @ 75421.27000000
#:   ETHUSDT  SELL LIMIT 0.00520000    @  2368.46000000
#:   USDCUSDT SELL LIMIT 5000.00000000 @     1.00030000
#:
#: (§125차's 69266.01 / 2248.56 / 1.00072 are superseded and must not reappear.)
#:
#: This tuple is the authority the runtime checks against.  The payload is
#: re-read at runtime so a doctored file is caught, but the payload can only
#: ever *match* this constant — it can never extend it.
D2_BOUND_ORDERS: Final[tuple[D2BoundOrder, ...]] = (
    D2BoundOrder(
        symbol="BTCUSDT",
        asset="BTC",
        side=D2_SIDE,
        order_type=D2_ORDER_TYPE,
        quantity=Decimal("0.00015000"),
        price=Decimal("75421.27000000"),
        time_in_force=D2_TIME_IN_FORCE,
        sealed_free_quantity=Decimal("0.00015957"),
        sealed_locked_quantity=Decimal("0"),
    ),
    D2BoundOrder(
        symbol="ETHUSDT",
        asset="ETH",
        side=D2_SIDE,
        order_type=D2_ORDER_TYPE,
        quantity=Decimal("0.00520000"),
        price=Decimal("2368.46000000"),
        time_in_force=D2_TIME_IN_FORCE,
        sealed_free_quantity=Decimal("0.00529470"),
        sealed_locked_quantity=Decimal("0"),
    ),
    D2BoundOrder(
        symbol="USDCUSDT",
        asset="USDC",
        side=D2_SIDE,
        order_type=D2_ORDER_TYPE,
        quantity=Decimal("5000.00000000"),
        price=Decimal("1.00030000"),
        time_in_force=D2_TIME_IN_FORCE,
        sealed_free_quantity=Decimal("5000.00000000"),
        sealed_locked_quantity=Decimal("0"),
    ),
)

#: ``allowed_operations`` from ``operator_contract.yaml``, in the same order as
#: :data:`D2_BOUND_ORDERS`.  Kept as a separate constant so the contract's own
#: wording is checkable against the code rather than only readable next to it.
D2_ALLOWED_OPERATION_IDS: Final[tuple[str, ...]] = (
    "BTCUSDT_sell_limit_bound_to_sealed_snapshot",
    "ETHUSDT_sell_limit_bound_to_sealed_snapshot",
    "USDCUSDT_sell_limit_conversion",
)


@dataclass(frozen=True, slots=True)
class SealedPayloadRecord:
    """One sealed payload this writer is willing to parse at all.

    Keyed by the SHA-256 of the file's exact bytes. ``dispatch_authorized``
    is the repository-level half of the dispatch gate: even a payload that
    claims an operator signature cannot dispatch unless a reviewed change has
    added its digest here with the flag set.
    """

    sha256: str
    pre_snapshot_hash: str
    dispatch_authorized: bool
    note: str


#: The complete set of sealed payloads this writer will parse.
#:
#: 🔴 Exactly one entry is ``dispatch_authorized=True``: the r8 execution
#: authority, under §134차.  The other two parse and then refuse — the r7
#: payload because its ``pre_snapshot_hash`` is superseded, the r8 binding
#: payload because it is unsigned.  Registration is *permission to parse*;
#: ``dispatch_authorized`` is the separate half, and it is checked against the
#: closed set :data:`D2_DISPATCH_AUTHORIZED_DIGESTS` at import.
D2_KNOWN_SEALED_PAYLOADS: Final[Mapping[str, SealedPayloadRecord]] = {
    "e1c2d250d73ae3bdb631289a7293c35c217b9e5c6e2694d3f8ea572d1835a3aa": (
        SealedPayloadRecord(
            sha256=("e1c2d250d73ae3bdb631289a7293c35c217b9e5c6e2694d3f8ea572d1835a3aa"),
            pre_snapshot_hash=D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH,
            dispatch_authorized=False,
            note=(
                "r7 attempt-2 binding payload, unsigned and superseded by "
                "§132차. Parseable so the refusal names 'superseded snapshot' "
                "rather than 'unknown digest'; it can never bind, because its "
                "pre_snapshot_hash is not D2_PRE_SNAPSHOT_HASH."
            ),
        )
    ),
    D2_R8_BINDING_PAYLOAD_SHA256: SealedPayloadRecord(
        sha256=D2_R8_BINDING_PAYLOAD_SHA256,
        pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
        dispatch_authorized=False,
        note=(
            "r8 attempt-2 binding payload, exactly as sealed: "
            "operator_authorization is null, expiry is null, and all three "
            "rows are mutation_authorized=false. It binds the three §132차 "
            "orders and authorizes none of them. It was deliberately not "
            "rewritten to carry an authorization it never had; the authority "
            "is the separate execution artifact below."
        ),
    ),
    D2_R8_EXECUTION_AUTHORITY_SHA256: SealedPayloadRecord(
        sha256=D2_R8_EXECUTION_AUTHORITY_SHA256,
        pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
        dispatch_authorized=True,
        note=(
            "r8 execution authority under operator decision §134차 "
            "(docs/contracts/d2-r8-execution-authority-20260821.json). "
            "Execution-only: it names the r8 seal by digest rather than "
            "restating it, and its scope is that seal alone."
        ),
    ),
}


def _assert_closed_order_set() -> None:
    """Import-time invariants over the constant itself.

    A future edit that adds a fourth row, flips a side, or introduces MARKET
    fails here rather than at a broker.
    """

    if len(D2_BOUND_ORDERS) != 3:
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            f"the D2 exception authorizes exactly 3 operations, "
            f"got {len(D2_BOUND_ORDERS)}",
        )
    if len(D2_ALLOWED_OPERATION_IDS) != len(D2_BOUND_ORDERS):
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            "allowed_operations and bound orders disagree in length",
        )
    symbols = tuple(order.symbol for order in D2_BOUND_ORDERS)
    if len(set(symbols)) != len(symbols):
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION, "duplicate symbol in bound order set"
        )
    client_ids = tuple(order.client_order_id for order in D2_BOUND_ORDERS)
    if len(set(client_ids)) != len(client_ids):
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            "derived client_order_ids collide",
        )
    for order, operation_id in zip(
        D2_BOUND_ORDERS, D2_ALLOWED_OPERATION_IDS, strict=True
    ):
        if order.side != D2_SIDE or order.order_type != D2_ORDER_TYPE:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"{order.symbol}: only {D2_SIDE}/{D2_ORDER_TYPE} is authorized",
            )
        if order.quantity <= 0 or order.price <= 0:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"{order.symbol}: non-positive quantity or price",
            )
        if order.quantity > order.sealed_free_quantity:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"{order.symbol}: sells more than the seal observed as free",
            )
        if not operation_id.startswith(f"{order.symbol}_"):
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"allowed_operation {operation_id!r} does not name {order.symbol}",
            )
    # 🔴 The §134차 tripwire, replacing the pre-cutover "nothing may dispatch"
    # form.  It is not weaker for being narrower: the comparison is closed
    # equality against a one-member set, so *adding* a second authorized digest
    # fails the import exactly as marking an unauthorized one does.  Widening
    # the permission therefore still costs a visible, reviewed diff in two
    # places — the registry and this constant.
    if D2_DISPATCH_AUTHORIZED_DIGESTS != frozenset({D2_R8_EXECUTION_AUTHORITY_SHA256}):
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            "§134차 authorizes exactly one digest — the r8 execution "
            f"authority — but D2_DISPATCH_AUTHORIZED_DIGESTS is "
            f"{sorted(D2_DISPATCH_AUTHORIZED_DIGESTS)}",
        )
    authorized = frozenset(
        digest
        for digest, record in D2_KNOWN_SEALED_PAYLOADS.items()
        if record.dispatch_authorized
    )
    if authorized != D2_DISPATCH_AUTHORIZED_DIGESTS:
        raise D2UnauthorizedOperation(
            D2ReasonCode.UNAUTHORIZED_OPERATION,
            "the set of dispatch_authorized sealed payloads must equal the "
            "§134차 permission exactly; §134차 reads 'that seal only, every "
            "other seal stays refused'. Registered as authorized: "
            f"{sorted(authorized)}; permitted: "
            f"{sorted(D2_DISPATCH_AUTHORIZED_DIGESTS)}",
        )
    for digest in sorted(authorized):
        record = D2_KNOWN_SEALED_PAYLOADS[digest]
        if record.sha256 != digest:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"authorized record {digest} carries sha256={record.sha256!r}",
            )
        if record.pre_snapshot_hash != D2_PRE_SNAPSHOT_HASH:
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                f"authorized record {digest} binds "
                f"{record.pre_snapshot_hash!r}, not the §132차 snapshot "
                f"{D2_PRE_SNAPSHOT_HASH!r}",
            )


_assert_closed_order_set()


# --------------------------------------------------------------------------
# Physical account identity and writer freeze
# --------------------------------------------------------------------------


def d2_physical_account_scope() -> PhysicalAccountScope:
    """The coordination scope of the shared Binance Demo physical account.

    Derived from the signed J2A registry entry — there is no caller-supplied
    scope string anywhere on this path.
    """

    return physical_account_scope_for_entry(get_lane_registry_entry(D2_LANE_ID))


def d2_advisory_keyset() -> tuple[int, ...]:
    """The single advisory key one D2 lease must hold."""

    return (d2_physical_account_scope().advisory_key,)


def d2_physical_account_id() -> str:
    """The canonical J2A identity bytes for the shared Binance Demo account."""

    entry = get_lane_registry_entry(D2_LANE_ID)
    physical_account_id = entry.physical_account_id
    if not isinstance(physical_account_id, str) or not physical_account_id.strip():
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ACCOUNT_MISMATCH,
            f"{D2_LANE_ID}: physical account identity is unknown",
        )
    return physical_account_id


def assert_registry_credential_fingerprint() -> None:
    """Cross-check the pinned fingerprint against the signed registry.

    Two-way on purpose. If the registry is re-pointed at a different Binance
    Demo account, this fails rather than letting a stale constant carry the
    one-shot to the wrong place; and if the constant is edited alone, the
    registry disagrees.
    """

    if D2_CREDENTIAL_FINGERPRINT not in d2_physical_account_id():
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ACCOUNT_MISMATCH,
            "the pinned D2 credential fingerprint does not appear in the "
            f"signed J2A physical account identity for {D2_LANE_ID}",
        )


def assert_writer_freeze() -> None:
    """No other lane may hold writer or autonomous authority on this account.

    Contract v2.1 §6 requires an account-wide writer freeze before account
    truth is taken. The signed registry is where that freeze is expressed, so
    this reads it rather than restating it: any lane sharing this physical
    account with ``writer`` or ``auto`` set means the freeze is not in force.
    """

    account = d2_physical_account_id()
    unfrozen = sorted(
        entry.lane_id
        for entry in CANONICAL_LANE_REGISTRY
        if entry.physical_account_id == account and (entry.writer or entry.auto)
    )
    if unfrozen:
        raise D2RemediationError(
            D2ReasonCode.WRITER_FREEZE_VIOLATED,
            "account-wide writer freeze is not in force; these lanes still "
            f"hold writer/auto authority on the same physical account: {unfrozen}",
        )


# --------------------------------------------------------------------------
# Sealed authority
# --------------------------------------------------------------------------


def _decimal(value: Any, *, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (DecimalException, TypeError, ValueError) as exc:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{what}: not a decimal ({value!r})"
        ) from exc


def _sealed_order(symbol: str, entry: Mapping[str, Any]) -> D2BoundOrder | None:
    """Map one sealed ``authorized_symbols.spot`` row onto a bound order.

    Returns ``None`` for rows that are not actionable (dust attestations and
    the USDT quote-cash row), so those can never become orders by accident.
    Anything actionable but malformed raises rather than being skipped — a
    silently dropped row would shrink the set without saying so.
    """

    if entry.get("disposition") != _SEALED_ACTIONABLE_DISPOSITION:
        return None
    step = entry.get("proposed_one_step")
    if not isinstance(step, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{symbol}: proposed_one_step missing"
        )
    time_in_force = step.get("time_in_force")
    if time_in_force is not None and time_in_force != D2_TIME_IN_FORCE:
        # The sealed payload leaves TIF null (it sealed a price/quantity, not a
        # wire format).  Null is accepted and D6's GTC is supplied here; any
        # *other* explicit value is a disagreement, not a default.
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ORDER_SET_MISMATCH,
            f"{symbol}: sealed time_in_force {time_in_force!r} is not "
            f"{D2_TIME_IN_FORCE!r}",
        )
    return D2BoundOrder(
        symbol=str(symbol),
        asset=str(entry.get("asset", "")),
        side=str(step.get("side", "")),
        order_type=str(step.get("order_type", "")),
        quantity=_decimal(step.get("proposed_quantity_floor"), what=f"{symbol} qty"),
        price=_decimal(step.get("proposed_limit_price_floor"), what=f"{symbol} price"),
        time_in_force=D2_TIME_IN_FORCE,
        sealed_free_quantity=_decimal(
            step.get("raw_free_quantity"), what=f"{symbol} free"
        ),
        sealed_locked_quantity=_decimal(
            step.get("raw_locked_quantity"), what=f"{symbol} locked"
        ),
    )


#: Spellings that a JSON serialiser, a client library, or an ``str()`` call
#: produces when the underlying value was absent. None of them is an
#: identifier, and treating any of them as one is how a malformed broker
#: response becomes "evidence".
_PSEUDO_NULL_TOKENS: Final[frozenset[str]] = frozenset(
    {"", "none", "null", "nil", "nan", "undefined", "<none>", "n/a", "-"}
)


def broker_identifier_problem(value: Any, *, field: str) -> str | None:
    """Return why ``value`` cannot serve as a broker identifier, or ``None``.

    Reads the **raw** response value, never a stringified DTO field. That
    distinction is the whole point: ``str(None)`` is ``'None'``, which is
    non-empty, non-blank, and passes every naive presence check — so a null
    ``orderId`` laundered through ``str()`` used to look like proof that an
    order exists.

    Booleans are rejected before the integer branch because ``bool`` is a
    subclass of ``int`` and ``True`` would otherwise read as id ``1``.
    """

    if value is None:
        return f"{field} is null"
    if isinstance(value, bool):
        return f"{field} is a boolean ({value!r}), not an identifier"
    if isinstance(value, int):
        # Binance order ids are positive int64 values; 0 and negatives are
        # sentinels, not orders.
        return None if value > 0 else f"{field} is {value!r}, not a positive id"
    if not isinstance(value, str):
        return f"{field} has non-identifier type {type(value).__name__}"
    if value.strip().lower() in _PSEUDO_NULL_TOKENS:
        return f"{field} is {value!r}, which is a spelling of absent"
    return None


def _describe(orders: Sequence[D2BoundOrder]) -> str:
    return (
        "["
        + ", ".join(
            f"{o.symbol} {o.side} {o.order_type} {format(o.quantity, 'f')}"
            f"@{format(o.price, 'f')} free={format(o.sealed_free_quantity, 'f')}"
            for o in sorted(orders, key=lambda o: o.symbol)
        )
        + "]"
    )


def bind_sealed_orders(payload: Mapping[str, Any]) -> tuple[D2BoundOrder, ...]:
    """Re-derive the order set from a parsed payload and prove it is *the* one.

    The return value is :data:`D2_BOUND_ORDERS` itself, never the parsed
    tuple.  That is deliberate: a parsing bug can then only ever cause a
    refusal, never a widened order set.

    This is the *shape* half of the gate.  It says nothing about whether the
    bytes are the sealed bytes or whether anyone authorized a dispatch; those
    are :func:`load_sealed_authority`'s job, and this function is not a
    sufficient precondition for anything.
    """

    if not isinstance(payload, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, "sealed payload is not a mapping"
        )
    if payload.get("pre_snapshot_hash") != D2_PRE_SNAPSHOT_HASH:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_HASH_MISMATCH,
            f"expected pre_snapshot_hash={D2_PRE_SNAPSHOT_HASH!r}, "
            f"got {payload.get('pre_snapshot_hash')!r}",
        )
    if payload.get("remediation_id") != D2_REMEDIATION_ID:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_IDENTITY_MISMATCH,
            f"expected remediation_id={D2_REMEDIATION_ID!r}, "
            f"got {payload.get('remediation_id')!r}",
        )
    if payload.get("product_domain") != D2_PRODUCT_DOMAIN:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_IDENTITY_MISMATCH,
            f"expected product_domain={D2_PRODUCT_DOMAIN!r}, "
            f"got {payload.get('product_domain')!r}",
        )

    authorized = payload.get("authorized_symbols")
    if not isinstance(authorized, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, "authorized_symbols missing"
        )
    spot = authorized.get("spot")
    if not isinstance(spot, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, "authorized_symbols.spot missing"
        )

    derived: list[D2BoundOrder] = []
    for symbol, entry in spot.items():
        if not isinstance(symbol, str) or not isinstance(entry, Mapping):
            continue
        order = _sealed_order(symbol, entry)
        if order is not None:
            derived.append(order)

    expected_by_symbol = {order.symbol: order for order in D2_BOUND_ORDERS}
    derived_by_symbol = {order.symbol: order for order in derived}
    if derived_by_symbol != expected_by_symbol:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ORDER_SET_MISMATCH,
            "sealed actionable set is not the bound set: "
            f"expected {_describe(D2_BOUND_ORDERS)}, got {_describe(tuple(derived))}",
        )
    return D2_BOUND_ORDERS


def _sealed_credential_fingerprint(payload: Mapping[str, Any]) -> str:
    identity = payload.get("physical_account_identity")
    if not isinstance(identity, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ACCOUNT_MISMATCH,
            "sealed payload carries no physical_account_identity",
        )
    fingerprint = identity.get("credential_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ACCOUNT_MISMATCH,
            "sealed payload carries no credential_fingerprint",
        )
    return fingerprint


def _mutation_authorized_symbols(payload: Mapping[str, Any]) -> frozenset[str]:
    authorized = payload.get("authorized_symbols")
    spot = authorized.get("spot") if isinstance(authorized, Mapping) else None
    if not isinstance(spot, Mapping):
        return frozenset()
    return frozenset(
        str(symbol)
        for symbol, entry in spot.items()
        if isinstance(entry, Mapping) and entry.get("mutation_authorized") is True
    )


def _parse_expiry(raw: Any) -> dt.datetime | None:
    if raw is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"expiry {raw!r} is not an ISO-8601 instant"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class SealedAuthority:
    """A sealed payload whose *bytes* were verified, plus what it authorizes.

    The constructor demands a module-private token, so the ordinary ways of
    producing one — a dict, a look-alike object, a hand-built instance — fail,
    and :func:`load_sealed_authority` is the only route that ends in a usable
    authority. That stops a caller from *accidentally* bypassing the digest
    check; it does not stop one that imports the token deliberately, and it is
    not offered as doing so.

    Binding and *authorization* are kept apart on purpose. A payload can bind
    perfectly — right hash, right three orders, right account — and still
    authorize nothing, which is exactly the state of the current r7 object.
    """

    source_path: str
    payload_sha256: str
    record: SealedPayloadRecord
    orders: tuple[D2BoundOrder, ...]
    credential_fingerprint: str
    operator_authorization: Any
    expiry: dt.datetime | None
    mutation_authorized_symbols: frozenset[str]
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _AUTHORITY_TOKEN:
            raise D2SealBindingMismatch(
                D2ReasonCode.SEAL_MALFORMED,
                "SealedAuthority may only be produced by load_sealed_authority",
            )

    def dispatch_block_reasons(self, *, now: dt.datetime) -> tuple[str, ...]:
        """Everything standing between this seal and a broker mutation.

        Reported in full rather than short-circuiting, so an operator reading a
        dry run sees the whole list instead of discovering the next blocker one
        run at a time.
        """

        reasons: list[str] = []
        if not self.record.dispatch_authorized:
            reasons.append(
                f"payload digest {self.payload_sha256} is registered as "
                f"dispatch_authorized=false ({self.record.note})"
            )
        if self.operator_authorization is None:
            reasons.append("operator_authorization is null — no re-sign is present")
        if self.expiry is None:
            reasons.append("expiry is absent — a one-shot authority must expire")
        elif self.expiry <= now:
            reasons.append(
                f"authority expired at {self.expiry.isoformat()} (now {now.isoformat()})"
            )
        bound = {order.symbol for order in self.orders}
        missing = sorted(bound - self.mutation_authorized_symbols)
        if missing:
            reasons.append(f"mutation_authorized is not true for {missing}")
        # The other direction, which used to go unchecked: a payload that
        # authorizes the three bound symbols *and a fourth* satisfied every
        # test above. The bound set can never grow, so the fourth could not
        # become an order — but a seal that claims more than it is allowed to
        # claim is not a seal this writer should act under at all.
        extra = sorted(self.mutation_authorized_symbols - bound)
        if extra:
            reasons.append(
                "mutation_authorized is true for symbols outside the bound "
                f"set: {extra}"
            )
        return tuple(reasons)

    @property
    def client_order_ids(self) -> frozenset[str]:
        return frozenset(order.client_order_id for order in self.orders)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "payload_sha256": self.payload_sha256,
            "pre_snapshot_hash": self.record.pre_snapshot_hash,
            "registered_dispatch_authorized": self.record.dispatch_authorized,
            "operator_authorization_present": self.operator_authorization is not None,
            "expiry": None if self.expiry is None else self.expiry.isoformat(),
            "mutation_authorized_symbols": sorted(self.mutation_authorized_symbols),
            "credential_fingerprint": self.credential_fingerprint,
        }


def _assert_execution_authority_shape(
    payload: Mapping[str, Any], *, source: Path
) -> None:
    """Re-check a dispatch-authorized payload's *contents*, not just its bytes.

    Only runs for a registry entry with ``dispatch_authorized=True``. For the
    shipped artifact this is redundant with the digest — the bytes cannot
    differ — and that redundancy is the point: if the registry is ever edited
    to authorize some other digest, these checks are what still refuse it. They
    are the content-level form of §134차's "그 외 봉인은 계속 거부한다".

    Nothing here reads the r8 snapshot payload. It re-reads the digests the
    authority *claims* to be bound to and compares them against the frozen
    §132차 / §134차 values, so an authority that names a different snapshot,
    manifest, or seal instant is refused.
    """

    def _require(field: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            raise D2SealBindingMismatch(
                D2ReasonCode.SEAL_IDENTITY_MISMATCH,
                f"{source}: dispatch-authorized artifact has {field}="
                f"{actual!r}, expected {expected!r}",
            )

    _require(
        "schema_version",
        payload.get("schema_version"),
        D2_EXECUTION_AUTHORITY_SCHEMA_VERSION,
    )
    _require("artifact_kind", payload.get("artifact_kind"), D2_EXECUTION_AUTHORITY_KIND)
    _require(
        "authority_scope", payload.get("authority_scope"), D2_EXECUTION_AUTHORITY_SCOPE
    )

    binding = payload.get("sealed_snapshot_binding")
    if not isinstance(binding, Mapping):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_IDENTITY_MISMATCH,
            f"{source}: dispatch-authorized artifact carries no "
            "sealed_snapshot_binding, so it names no seal",
        )
    # ① of §134차: the r8 snapshot payload keeps its own bytes. An authority
    # that admits to having rewritten it is refused rather than trusted.
    _require(
        "sealed_snapshot_binding.snapshot_payload_rewritten",
        binding.get("snapshot_payload_rewritten"),
        False,
    )
    _require(
        "sealed_snapshot_binding.binding_payload_sha256",
        binding.get("binding_payload_sha256"),
        D2_R8_BINDING_PAYLOAD_SHA256,
    )
    _require(
        "sealed_snapshot_binding.artifact_manifest_sha256",
        binding.get("artifact_manifest_sha256"),
        D2_R8_ARTIFACT_MANIFEST_SHA256,
    )
    _require(
        "sealed_snapshot_binding.snapshot_seal_sha256",
        binding.get("snapshot_seal_sha256"),
        D2_SNAPSHOT_SEAL_SHA256,
    )
    _require(
        "sealed_snapshot_binding.seal_timestamp_utc",
        binding.get("seal_timestamp_utc"),
        D2_R8_SEAL_TIMESTAMP_UTC,
    )


def load_sealed_authority(path: str | Path) -> SealedAuthority:
    """Read a sealed payload from disk and verify it end to end.

    The digest is computed over the file's **exact bytes**, before the JSON is
    parsed, and compared against :data:`D2_KNOWN_SEALED_PAYLOADS`. An unknown
    digest is refused outright — not downgraded to a warning, and not rescued
    by the contents looking right, because "the contents look right" is
    precisely what a doctored file arranges.
    """

    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{source}: cannot read sealed payload: {exc}"
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    record = D2_KNOWN_SEALED_PAYLOADS.get(digest)
    if record is None:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_UNKNOWN_DIGEST,
            f"{source}: sha256={digest} is not a registered sealed payload. "
            f"Registered: {sorted(D2_KNOWN_SEALED_PAYLOADS)}",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{source}: not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_MALFORMED, f"{source}: sealed payload is not an object"
        )

    orders = bind_sealed_orders(payload)
    if record.pre_snapshot_hash != payload.get("pre_snapshot_hash"):
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_HASH_MISMATCH,
            "registered digest and payload disagree on pre_snapshot_hash",
        )

    if record.dispatch_authorized:
        _assert_execution_authority_shape(payload, source=source)

    assert_registry_credential_fingerprint()
    fingerprint = _sealed_credential_fingerprint(payload)
    if fingerprint != D2_CREDENTIAL_FINGERPRINT:
        raise D2SealBindingMismatch(
            D2ReasonCode.SEAL_ACCOUNT_MISMATCH,
            f"sealed credential_fingerprint {fingerprint!r} is not the shared "
            "Binance Demo account this one-shot is bound to",
        )

    return SealedAuthority(
        source_path=str(source),
        payload_sha256=digest,
        record=record,
        orders=orders,
        credential_fingerprint=fingerprint,
        operator_authorization=payload.get("operator_authorization"),
        expiry=_parse_expiry(payload.get("expiry")),
        mutation_authorized_symbols=_mutation_authorized_symbols(payload),
        _token=_AUTHORITY_TOKEN,
    )


# --------------------------------------------------------------------------
# Lease
# --------------------------------------------------------------------------


async def acquire_d2_lease(*, engine: AsyncEngine) -> PostgresAdvisoryKeysetLease:
    """Take the account-wide J3A lease this writer refuses to run without.

    One key, because spot, sidecar, and futures all derive the same
    physical-account scope: the Binance Demo credentials name one account, so
    one lease covers the whole of it.

    The usual J3A caveat applies and is not softened here — the lease
    coordinates processes *inside this repository*. Binance never sees it, so
    an operator at a console or an out-of-repo process reaches the same account
    without contending for it. It is why the writer also re-attests before each
    submit and reads the account-wide open-order book before dispatching.
    """

    async def _factory() -> LockAuthorityConnection:
        connection = await engine.connect()
        return SqlAlchemyLockAuthority(
            connection, observer_factory=lambda: engine.connect()
        )

    return await acquire_physical_account_lease(
        keys=d2_advisory_keyset(), connection_factory=_factory
    )


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class D2PlannedOperation:
    """One composed request, before anything is dispatched."""

    operation_id: str
    client_order_id: str
    order: D2BoundOrder
    request_params: dict[str, str]


@dataclass(frozen=True, slots=True)
class D2AccountTruth:
    """One pre-dispatch bounded observation of the whole account."""

    observed_at_utc: str
    open_orders: tuple[dict[str, str], ...]
    balances: tuple[dict[str, str], ...]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-account-truth.v1",
            "observed_at_utc": self.observed_at_utc,
            "account_wide_open_orders": list(self.open_orders),
            "balances": list(self.balances),
        }


@dataclass(frozen=True, slots=True)
class D2DryRunReport:
    """Full-path rehearsal that stops immediately before the signed POST."""

    remediation_id: str
    pre_snapshot_hash: str
    writer: str
    venue_host: str
    lease_attested: bool
    authority: dict[str, Any]
    dispatch_block_reasons: tuple[str, ...]
    operations: tuple[D2PlannedOperation, ...]
    account_truth: D2AccountTruth | None = None
    order_test_results: tuple[SpotDemoOrderTestResult, ...] = ()
    broker_mutation_count: int = 0

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-dry-run.v1",
            "writer": self.writer,
            "remediation_id": self.remediation_id,
            "pre_snapshot_hash": self.pre_snapshot_hash,
            "venue_host": self.venue_host,
            "lease_attested": self.lease_attested,
            "authority": self.authority,
            "dispatch_authorized": not self.dispatch_block_reasons,
            "dispatch_block_reasons": list(self.dispatch_block_reasons),
            "broker_mutation_count": self.broker_mutation_count,
            "order_test_count": len(self.order_test_results),
            "account_truth": (
                None if self.account_truth is None else self.account_truth.as_evidence()
            ),
            "operations": [
                {
                    "operation_id": op.operation_id,
                    "client_order_id": op.client_order_id,
                    "request_params": op.request_params,
                }
                for op in self.operations
            ],
        }


@dataclass(frozen=True, slots=True)
class D2ProofEpoch:
    """One bounded post-dispatch observation.

    Two of these, collected independently while the lease is still held,
    are the contract's "dual independent proof".
    """

    epoch_index: int
    observed_at_utc: str
    open_orders: tuple[dict[str, str], ...]
    balances: tuple[dict[str, str], ...]
    ledger_states: dict[str, str]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-proof-epoch.v1",
            "epoch_index": self.epoch_index,
            "observed_at_utc": self.observed_at_utc,
            "account_wide_open_orders": list(self.open_orders),
            "balances": list(self.balances),
            "ledger_states": dict(self.ledger_states),
        }


@dataclass(frozen=True, slots=True)
class D2DispatchOutcome:
    """What happened to one authorized operation."""

    operation_id: str
    client_order_id: str
    order: D2BoundOrder
    request_params: dict[str, str]
    status: str
    broker_order_id: str | None = None
    ledger_state: str = ""
    readback_used: bool = False
    anomaly_reason: str | None = None


@dataclass(frozen=True, slots=True)
class D2ExecutionReport:
    remediation_id: str
    pre_snapshot_hash: str
    writer: str
    authority: dict[str, Any]
    outcomes: tuple[D2DispatchOutcome, ...]
    proof_epochs: tuple[D2ProofEpoch, ...]
    broker_submit_count: int
    account_truth: D2AccountTruth | None = None
    halted_reason: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "d2-remediation-single-execution.v1",
            "writer": self.writer,
            "remediation_id": self.remediation_id,
            "pre_snapshot_hash": self.pre_snapshot_hash,
            "authority": self.authority,
            "broker_submit_count": self.broker_submit_count,
            "halted_reason": self.halted_reason,
            "account_truth": (
                None if self.account_truth is None else self.account_truth.as_evidence()
            ),
            "outcomes": [
                {
                    "operation_id": o.operation_id,
                    "client_order_id": o.client_order_id,
                    "request_params": o.request_params,
                    "status": o.status,
                    "broker_order_id": o.broker_order_id,
                    "ledger_state": o.ledger_state,
                    "readback_used": o.readback_used,
                    "anomaly_reason": o.anomaly_reason,
                }
                for o in self.outcomes
            ],
            "proof_epochs": [epoch.as_evidence() for epoch in self.proof_epochs],
        }


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def d2_remediation_enabled() -> bool:
    """The dedicated, additional, default-off gate for this writer.

    Reads ``os.environ`` and nothing else. An earlier revision accepted an
    ``environ`` mapping for testability, which meant an in-process caller could
    arm the gate the operator had deliberately left off — a test seam that was
    also a bypass. Tests use ``monkeypatch.setenv``, which is the real thing.
    """

    return _truthy(os.environ.get(D2_REMEDIATION_ENABLED_ENV))


# --------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------


@dataclass
class _DispatchClaims:
    """One dispatch per client_order_id within this process.

    The *inner* of two fences, and the weaker one. It cannot survive a restart,
    which is precisely the case the durable ledger check exists for; keeping it
    anyway means a bug that bypasses the ledger lookup still cannot double-send
    inside one run.
    """

    claimed: set[str] = field(default_factory=set)

    def claim(self, client_order_id: str) -> None:
        if client_order_id in self.claimed:
            raise D2BlindRetryRefused(
                D2ReasonCode.BLIND_RETRY_REFUSED,
                f"client_order_id={client_order_id!r} was already dispatched; "
                "an ambiguous outcome is resolved by readback, never by "
                "re-sending",
            )
        self.claimed.add(client_order_id)


class D2RemediationSingleWriter:
    """The ``d2_remediation_single`` writer.

    Construction takes a verified :class:`SealedAuthority`, a real
    :class:`PostgresAdvisoryKeysetLease`, and a real
    :class:`BinanceDemoLedgerService`. None of the three has a permissive
    default, and none can be substituted by a look-alike.
    """

    writer_name: Final[str] = WRITER_NAME

    def __init__(
        self,
        *,
        execution_client: BinanceSpotDemoExecutionClient,
        authority: SealedAuthority,
        lease: PostgresAdvisoryKeysetLease,
        lease_grant: AdvisoryLeaseGrant,
        ledger: BinanceDemoLedgerService,
        now_fn: Any = None,
    ) -> None:
        if not d2_remediation_enabled():
            raise D2RemediationDisabled(
                D2ReasonCode.DISABLED,
                f"{D2_REMEDIATION_ENABLED_ENV} is not truthy in the process "
                "environment; this one-shot writer is default-disabled "
                "independently of BINANCE_SPOT_DEMO_ENABLED",
            )
        if not isinstance(authority, SealedAuthority):
            raise D2SealBindingMismatch(
                D2ReasonCode.SEAL_MALFORMED,
                "orders must arrive as a SealedAuthority from "
                "load_sealed_authority(); a raw payload or a look-alike is "
                "not accepted",
            )
        if not isinstance(lease, PostgresAdvisoryKeysetLease):
            # A duck-typed stand-in with `.released` and `.assert_owned` would
            # otherwise satisfy every later check. This rejects the wrong
            # *type*; it is not a provenance check, and the class can still be
            # constructed over a fabricated lock authority in-process.
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_NOT_A_CAPABILITY,
                "lease must be a real PostgresAdvisoryKeysetLease; a duck-typed "
                f"{type(lease).__name__} is not one",
            )
        if not isinstance(ledger, BinanceDemoLedgerService):
            raise D2LedgerRequired(
                D2ReasonCode.LEDGER_REQUIRED,
                "a real BinanceDemoLedgerService is required; there is no "
                "ledger-less dispatch path",
            )
        # Re-assert the host here even though the transport already pins it:
        # this module composes signed order params, so it proves for itself
        # that they are going to the Spot Demo host.
        self._assert_spot_demo_endpoint(execution_client)
        self._assert_client_account(execution_client, authority)
        assert_writer_freeze()
        self._authority = authority
        # The dispatch set is the module constant, never the parsed tuple.
        self._orders = D2_BOUND_ORDERS
        self._client = execution_client
        self._ledger = ledger
        self._lease = lease
        self._lease_grant = lease_grant
        self._now_fn = now_fn or (lambda: dt.datetime.now(dt.UTC))
        self._claims = _DispatchClaims()
        self._submit_count = 0

    # -- preconditions -------------------------------------------------

    @staticmethod
    def _assert_spot_demo_endpoint(
        execution_client: BinanceSpotDemoExecutionClient,
    ) -> None:
        base_url = getattr(execution_client, "_base_url", "")
        host = urlsplit(str(base_url)).hostname or ""
        try:
            assert_spot_demo_host(host)
        except Exception as exc:
            raise D2UnauthorizedOperation(
                D2ReasonCode.HOST_NOT_SPOT_DEMO,
                f"execution client base_url host {host!r} is not a Spot Demo host",
            ) from exc

    @staticmethod
    def _assert_client_account(
        execution_client: BinanceSpotDemoExecutionClient,
        authority: SealedAuthority,
    ) -> None:
        """The live credentials must be the account the seal observed.

        The Spot Demo host is shared by several demo lanes, so "the host is
        right" says nothing about *whose* account is about to be sold from.
        """

        live = getattr(execution_client, "credential_fingerprint", None)
        if live != authority.credential_fingerprint:
            raise D2SealBindingMismatch(
                D2ReasonCode.SEAL_ACCOUNT_MISMATCH,
                "the execution client's credential fingerprint does not match "
                "the physical account the seal was taken from",
            )

    async def _require_lease(self) -> None:
        """Re-prove the lease immediately before every broker mutation.

        Acquisition-time success is not carried forward: a transparently
        reconnected session owns nothing, and ``assert_owned`` is what
        notices.
        """

        lease = self._lease
        if lease.released:
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_NOT_HELD,
                "the J3A physical-account lease is released; the D2 order path "
                "is unreachable without one",
            )
        expected_keys = d2_advisory_keyset()
        if tuple(self._lease_grant.keys) != expected_keys:
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_SCOPE_MISMATCH,
                f"lease grant covers {self._lease_grant.keys!r}, but the "
                f"Binance Demo physical account needs {expected_keys!r}",
            )
        try:
            await lease.assert_owned(self._lease_grant)
        except D2RemediationError:  # pragma: no cover - defensive
            raise
        except Exception as exc:
            raise D2LeaseNotHeld(
                D2ReasonCode.LEASE_NOT_HELD,
                f"lease ownership could not be re-attested: {exc}",
            ) from exc

    def dispatch_block_reasons(self) -> tuple[str, ...]:
        """Evaluate the authority against the **real** clock.

        Deliberately not ``self._now_fn``. That hook exists so evidence
        timestamps are reproducible in tests, and letting it feed the expiry
        comparison would turn a test seam into a bypass: a caller passing a
        ``now_fn`` fixed in the past could revive an expired one-shot.
        """

        return self._authority.dispatch_block_reasons(now=dt.datetime.now(dt.UTC))

    def _require_dispatch_authority(self) -> None:
        reasons = self.dispatch_block_reasons()
        if reasons:
            raise D2DispatchNotAuthorized(
                D2ReasonCode.DISPATCH_NOT_AUTHORIZED,
                "nothing authorizes a broker mutation under this seal: "
                + "; ".join(reasons),
            )

    # -- planning ------------------------------------------------------

    def plan(self) -> tuple[D2PlannedOperation, ...]:
        """Compose the three requests. Pure — no lease, no I/O, no signing.

        Deterministic: calling it twice, or in two processes, yields the same
        ``client_order_id`` for the same bound order.
        """

        return tuple(
            D2PlannedOperation(
                operation_id=operation_id,
                client_order_id=order.client_order_id,
                order=order,
                request_params=order.request_params(),
            )
            for order, operation_id in zip(
                self._orders, D2_ALLOWED_OPERATION_IDS, strict=True
            )
        )

    # -- account truth -------------------------------------------------

    async def collect_account_truth(
        self, *, expected_client_order_ids: frozenset[str]
    ) -> D2AccountTruth:
        """Read the whole account and refuse if it is not what the seal saw.

        Account-wide, not symbol-scoped: a symbol-filtered read cannot answer
        "is anything else resting on this shared account?", and the Demo
        credentials are shared with other lanes.

        Runs *before* the first dispatch. The earlier revision read this state
        only in the post-dispatch proof epochs, which meant a foreign resting
        order or a drifted balance was discovered after the orders were already
        on the book.
        """

        open_orders = await self._client.get_all_open_orders()
        foreign = sorted(
            f"{entry.symbol}:{entry.client_order_id}"
            for entry in open_orders.orders
            if entry.client_order_id not in expected_client_order_ids
        )
        if foreign:
            raise D2AccountTruthDrift(
                D2ReasonCode.ACCOUNT_TRUTH_DRIFT,
                "the shared Demo account has resting orders this one-shot did "
                f"not place: {foreign}. Contract v2.1 §6 requires open-order "
                "cancellation or terminal proof before exposure reduction, and "
                "this writer has no cancel path — clear them first.",
            )

        balances: list[dict[str, str]] = []
        drift: list[str] = []
        for order in self._orders:
            balance = await self._client.get_asset_balance(asset=order.asset)
            balances.append(
                {
                    "asset": balance.asset,
                    "free": format(balance.free, "f"),
                    "locked": format(balance.locked, "f"),
                }
            )
            if balance.free != order.sealed_free_quantity:
                drift.append(
                    f"{order.asset} free {format(balance.free, 'f')} != sealed "
                    f"{format(order.sealed_free_quantity, 'f')}"
                )
            if balance.locked != order.sealed_locked_quantity:
                drift.append(
                    f"{order.asset} locked {format(balance.locked, 'f')} != sealed "
                    f"{format(order.sealed_locked_quantity, 'f')}"
                )
        if drift:
            raise D2AccountTruthDrift(
                D2ReasonCode.ACCOUNT_TRUTH_DRIFT,
                "live balances have drifted from the sealed observation, so the "
                f"seal no longer describes this account: {drift}",
            )
        return D2AccountTruth(
            observed_at_utc=self._now_fn().isoformat(),
            open_orders=tuple(
                {
                    "symbol": entry.symbol,
                    "side": entry.side,
                    "client_order_id": entry.client_order_id,
                    "qty": format(entry.qty, "f"),
                    "status": entry.status,
                }
                for entry in open_orders.orders
            ),
            balances=tuple(balances),
        )

    # -- execution -----------------------------------------------------

    async def execute(
        self,
        *,
        confirm: bool = False,
        include_order_test: bool = True,
    ) -> D2DryRunReport | D2ExecutionReport:
        """Run the writer. ``confirm=False`` (the default) mutates nothing.

        The dry run walks the *whole* path — env gate, host re-assertion,
        account identity, writer freeze, seal binding, lease attestation,
        pre-dispatch account truth, request composition, and the non-mutating
        ``POST /api/v3/order/test`` shape check — and stops there. It reports
        what blocks dispatch rather than raising, because listing the blockers
        is the point of a rehearsal.

        ``confirm=True`` requires the authority to be complete; if it is not,
        it raises before any lease-protected work.
        """

        await self._require_lease()
        operations = self.plan()
        if not confirm:
            return await self._dry_run(
                operations, include_order_test=include_order_test
            )
        # Under confirm, the order-shape check is not optional: it is the only
        # broker-side proof that the sealed filters still describe the market.
        self._require_dispatch_authority()
        return await self._confirmed_run(operations)

    async def _dry_run(
        self,
        operations: tuple[D2PlannedOperation, ...],
        *,
        include_order_test: bool,
    ) -> D2DryRunReport:
        truth = await self.collect_account_truth(
            expected_client_order_ids=self._authority.client_order_ids
        )
        tests: list[SpotDemoOrderTestResult] = []
        if include_order_test:
            for op in operations:
                tests.append(
                    await self._client.order_test(
                        symbol=op.order.symbol,
                        side=op.order.side,
                        order_type=op.order.order_type,
                        qty=op.order.quantity,
                        price=op.order.price,
                        time_in_force=op.order.time_in_force,
                    )
                )
        return D2DryRunReport(
            remediation_id=D2_REMEDIATION_ID,
            pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
            writer=WRITER_NAME,
            venue_host=D2_VENUE_HOST,
            lease_attested=True,
            authority=self._authority.as_evidence(),
            dispatch_block_reasons=self.dispatch_block_reasons(),
            operations=operations,
            account_truth=truth,
            order_test_results=tuple(tests),
            broker_mutation_count=0,
        )

    async def _confirmed_run(
        self, operations: tuple[D2PlannedOperation, ...]
    ) -> D2ExecutionReport:
        truth = await self.collect_account_truth(
            expected_client_order_ids=self._authority.client_order_ids
        )
        outcomes: list[D2DispatchOutcome] = []
        halted_reason: str | None = None
        for op in operations:
            outcome = await self._dispatch_one(op)
            outcomes.append(outcome)
            if outcome.status in {"anomaly", "unknown"}:
                # An unestablished outcome stops the run. Continuing would mean
                # mutating an account whose current state is not known.
                halted_reason = (
                    f"{op.operation_id}: {outcome.anomaly_reason or outcome.status}"
                )
                break
        epochs = await self._collect_proof_epochs(outcomes)
        return D2ExecutionReport(
            remediation_id=D2_REMEDIATION_ID,
            pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
            writer=WRITER_NAME,
            authority=self._authority.as_evidence(),
            outcomes=tuple(outcomes),
            proof_epochs=epochs,
            broker_submit_count=self._submit_count,
            account_truth=truth,
            halted_reason=halted_reason,
        )

    async def _dispatch_one(self, op: D2PlannedOperation) -> D2DispatchOutcome:
        order = op.order
        # The durable fence, read through an independent transaction so it sees
        # what *other processes* committed rather than this session's own
        # uncommitted work: has this exact bound order been attempted before?
        prior = await self._ledger.committed_lifecycle_state(op.client_order_id)
        if prior is not None:
            return await self._resolve_prior_attempt(op, prior_state=prior)

        instrument_id = await self._ledger.resolve_or_create_instrument(
            venue=D2_VENUE,
            product=D2_PRODUCT,
            venue_symbol=order.symbol,
            base_asset=order.asset,
            quote_asset=D2_QUOTE_ASSET,
        )
        # Record, then execute — never the other way round. The claim is
        # committed in its own transaction *before* any broker call, so a
        # process that dies between the POST and its own commit still leaves the
        # row a restart needs. Writing into the caller's transaction and
        # committing afterwards, as this did before, meant exactly that crash
        # window erased the fence and the restart re-sent.
        await self._ledger.commit_planned_claim(
            instrument_id=instrument_id,
            product=D2_PRODUCT,
            venue_host=D2_VENUE_HOST,
            client_order_id=op.client_order_id,
            side=order.side,
            order_type=order.order_type,
            qty=order.quantity,
            price=order.price,
            extra_metadata=self._evidence_metadata(op),
            now=self._now_fn(),
        )
        # Prove the claim is durable by reading it back out of a *different*
        # transaction. A ledger whose writes go nowhere fails here, before the
        # broker is touched: requiring the type was never the same as requiring
        # the effect.
        observed = await self._ledger.committed_lifecycle_state(op.client_order_id)
        if observed != "planned":
            raise D2ClaimNotDurable(
                D2ReasonCode.CLAIM_NOT_DURABLE,
                f"{op.operation_id}: the pre-send claim for "
                f"{op.client_order_id!r} is not visible outside its own "
                f"transaction (read back {observed!r}). Refusing to send an "
                "order no restart could recognise.",
            )
        await self._ledger.record_previewed(
            client_order_id=op.client_order_id, now=self._now_fn()
        )
        await self._client.order_test(
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            qty=order.quantity,
            price=order.price,
            time_in_force=order.time_in_force,
        )
        await self._ledger.record_validated(
            client_order_id=op.client_order_id, now=self._now_fn()
        )

        # Re-prove the lease immediately before the signed POST, not once at
        # the top of the run.
        await self._require_lease()
        self._claims.claim(op.client_order_id)
        self._submit_count += 1
        try:
            result = await self._client.submit_order(
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                qty=order.quantity,
                price=order.price,
                time_in_force=order.time_in_force,
                client_order_id=op.client_order_id,
                confirm=True,
            )
        except Exception as exc:
            return await self._resolve_by_readback(op, submit_error=exc)
        if isinstance(result, SpotDemoDryRunResult):  # pragma: no cover - defensive
            raise D2UnauthorizedOperation(
                D2ReasonCode.UNAUTHORIZED_OPERATION,
                "submit_order returned a dry-run sentinel under confirm=True",
            )
        self._assert_echo(op, result)
        await self._ledger.record_submitted(
            client_order_id=op.client_order_id,
            broker_order_id=result.broker_order_id,
            now=self._now_fn(),
        )
        return D2DispatchOutcome(
            operation_id=op.operation_id,
            client_order_id=op.client_order_id,
            order=order,
            request_params=op.request_params,
            status=str(result.status),
            broker_order_id=result.broker_order_id,
            ledger_state="submitted",
        )

    async def _resolve_prior_attempt(
        self, op: D2PlannedOperation, *, prior_state: str
    ) -> D2DispatchOutcome:
        """A ledger row already exists for this bound order — do not send.

        This is the case a process-local claim set cannot see: the first
        attempt may have happened in a process that has since died. The only
        safe move is to ask the broker what became of it.
        """

        try:
            status_body = await self._client.get_order_status(
                symbol=op.order.symbol, client_order_id=op.client_order_id
            )
        except BinanceDemoOrderNotFound as exc:
            raise D2PriorAttemptUnresolved(
                D2ReasonCode.PRIOR_ATTEMPT_UNRESOLVED,
                f"{op.operation_id}: the ledger records a prior attempt in state "
                f"{prior_state!r} but the broker does not know "
                f"{op.client_order_id!r}. That is unresolvable from here — it "
                "could equally mean the order never arrived or that it arrived "
                "and was removed. Refusing to re-send.",
            ) from exc
        except Exception as exc:
            raise D2PriorAttemptUnresolved(
                D2ReasonCode.PRIOR_ATTEMPT_UNRESOLVED,
                f"{op.operation_id}: a prior attempt exists in state "
                f"{prior_state!r} and its outcome could not be read back: {exc!r}",
            ) from exc
        echoed = self._echo_from_status(op, status_body)
        self._assert_echo(op, echoed)
        return D2DispatchOutcome(
            operation_id=op.operation_id,
            client_order_id=op.client_order_id,
            order=op.order,
            request_params=op.request_params,
            status=echoed.status,
            broker_order_id=echoed.broker_order_id,
            ledger_state=prior_state,
            readback_used=True,
        )

    def _echo_from_status(
        self, op: D2PlannedOperation, status_body: Mapping[str, Any]
    ) -> SpotDemoOrderSubmitResult:
        # No default for clientOrderId, and no ``str()`` around a possibly-null
        # orderId. Substituting the id we asked about, or turning ``None`` into
        # the four-character string ``'None'``, manufactures the very evidence
        # the echo check exists to demand. Absent stays absent here; the check
        # below is what decides.
        raw_cid = status_body.get("clientOrderId")
        raw_oid = status_body.get("orderId")
        return SpotDemoOrderSubmitResult(
            client_order_id="" if raw_cid is None else str(raw_cid),
            broker_order_id="" if raw_oid is None else str(raw_oid),
            symbol=str(status_body.get("symbol", "")),
            side=str(status_body.get("side", "")),
            order_type=str(status_body.get("type", "")),
            qty=_decimal(status_body.get("origQty", "0"), what="readback origQty"),
            executed_qty=_decimal(
                status_body.get("executedQty", "0"), what="readback executedQty"
            ),
            cummulative_quote_qty=Decimal("0"),
            status=str(status_body.get("status", "UNKNOWN")),
            raw_response_redacted=dict(status_body),
        )

    def _assert_echo(
        self, op: D2PlannedOperation, result: SpotDemoOrderSubmitResult
    ) -> None:
        """The broker must echo back exactly what was authorized.

        A response describing a different symbol, side, type, quantity, price,
        or time-in-force is not a success with cosmetic drift; it means the
        account was mutated in a way the seal does not cover.

        Price and time-in-force live in the raw response rather than in typed
        DTO fields, so they are read from there. Their *absence* is a failure,
        not a pass: a response that cannot prove the sealed price has not
        proved it.

        The same applies to *which order* the response is about. Matching
        contents are not identity — the shared Demo account can carry another
        order with the same symbol, side, quantity, and price — so the echoed
        ``clientOrderId`` must equal the deterministic id we sent, and a broker
        order id must be present. Without both, this response might describe
        someone else's order and we would be booking it as ours.

        Both identifiers are read from the **raw** body through
        :func:`broker_identifier_problem`, not from the stringified DTO fields.
        A null ``orderId`` that has been through ``str()`` arrives as the
        four-character string ``'None'`` and passes any "is it non-empty"
        test; the same goes for ``'null'``, ``''``, whitespace, and ``0``.
        Those are spellings of absent, and absent is a failure.
        """

        order = op.order
        raw = result.raw_response_redacted or {}
        mismatches: list[str] = []
        if result.client_order_id != op.client_order_id:
            mismatches.append(
                f"clientOrderId {result.client_order_id!r} != {op.client_order_id!r}"
            )
        cid_problem = broker_identifier_problem(
            raw.get("clientOrderId"), field="clientOrderId"
        )
        if cid_problem is not None:
            mismatches.append(
                f"{cid_problem} — this response is not proven to be about our order"
            )
        elif str(raw.get("clientOrderId")) != op.client_order_id:
            mismatches.append(
                f"raw clientOrderId {raw.get('clientOrderId')!r} != "
                f"{op.client_order_id!r}"
            )
        oid_problem = broker_identifier_problem(raw.get("orderId"), field="orderId")
        if oid_problem is not None:
            mismatches.append(
                f"{oid_problem} — a fill has no evidence without a broker order id"
            )
        if result.symbol != order.symbol:
            mismatches.append(f"symbol {result.symbol!r} != {order.symbol!r}")
        if result.side != order.side:
            mismatches.append(f"side {result.side!r} != {order.side!r}")
        if result.order_type != order.order_type:
            mismatches.append(f"type {result.order_type!r} != {order.order_type!r}")
        if result.qty != order.quantity:
            mismatches.append(f"qty {result.qty} != {order.quantity}")
        if result.executed_qty > order.quantity:
            mismatches.append(
                f"executedQty {result.executed_qty} exceeds authorized {order.quantity}"
            )
        echoed_price = raw.get("price")
        if echoed_price is None:
            mismatches.append(
                "broker response carries no price — the sealed limit price is unproven"
            )
        else:
            try:
                if Decimal(str(echoed_price)) != order.price:
                    mismatches.append(f"price {echoed_price!r} != {order.price}")
            except (DecimalException, TypeError, ValueError):
                mismatches.append(f"price {echoed_price!r} is not a decimal")
        echoed_tif = raw.get("timeInForce")
        if echoed_tif is None:
            mismatches.append(
                "broker response carries no timeInForce — GTC is unproven"
            )
        elif str(echoed_tif) != order.time_in_force:
            mismatches.append(f"timeInForce {echoed_tif!r} != {order.time_in_force!r}")
        if mismatches:
            raise D2RemediationError(
                D2ReasonCode.BROKER_ECHO_MISMATCH,
                f"{op.operation_id}: " + "; ".join(mismatches),
            )

    async def _resolve_by_readback(
        self, op: D2PlannedOperation, *, submit_error: BaseException
    ) -> D2DispatchOutcome:
        """Establish an ambiguous dispatch's outcome by *asking*, not re-sending.

        A local failure after a signed POST says nothing about whether the
        order reached Binance. The only safe move is one bounded read of the
        order by its client id. Anything short of a positive answer is an
        anomaly, because "not found" cannot be distinguished from "not yet
        visible" and both are outranked by the cost of a duplicate order.
        """

        try:
            status_body = await self._client.get_order_status(
                symbol=op.order.symbol, client_order_id=op.client_order_id
            )
        except BinanceDemoOrderNotFound:
            reason = (
                "submit_outcome_unknown_order_not_found_after_ambiguous_submit: "
                f"{submit_error!r}"
            )
            return await self._anomaly_outcome(op, reason)
        except Exception as readback_error:
            reason = (
                f"submit_outcome_unknown_readback_failed: {readback_error!r} "
                f"(original: {submit_error!r})"
            )
            return await self._anomaly_outcome(op, reason)

        echoed = self._echo_from_status(op, status_body)
        self._assert_echo(op, echoed)
        await self._ledger.record_submitted(
            client_order_id=op.client_order_id,
            broker_order_id=echoed.broker_order_id,
            now=self._now_fn(),
        )
        return D2DispatchOutcome(
            operation_id=op.operation_id,
            client_order_id=op.client_order_id,
            order=op.order,
            request_params=op.request_params,
            status=echoed.status,
            broker_order_id=echoed.broker_order_id,
            ledger_state="submitted",
            readback_used=True,
        )

    async def _anomaly_outcome(
        self, op: D2PlannedOperation, reason: str
    ) -> D2DispatchOutcome:
        await self._ledger.record_anomaly(
            client_order_id=op.client_order_id,
            reason=reason,
            now=self._now_fn(),
        )
        return D2DispatchOutcome(
            operation_id=op.operation_id,
            client_order_id=op.client_order_id,
            order=op.order,
            request_params=op.request_params,
            status="anomaly",
            ledger_state="anomaly",
            readback_used=True,
            anomaly_reason=reason,
        )

    # -- proof epochs --------------------------------------------------

    async def _collect_proof_epochs(
        self, outcomes: Sequence[D2DispatchOutcome]
    ) -> tuple[D2ProofEpoch, ...]:
        """Two independent bounded observations, lease still held.

        Each epoch issues its own account-wide open-order read and its own
        per-asset balance reads; neither reuses the other's bytes.
        """

        epochs: list[D2ProofEpoch] = []
        for index in (1, 2):
            await self._require_lease()
            epochs.append(await self.collect_proof_epoch(index, outcomes))
        if len(epochs) != 2:  # pragma: no cover - defensive
            raise D2RemediationError(
                D2ReasonCode.PROOF_EPOCHS_INCOMPLETE,
                f"expected 2 proof epochs, got {len(epochs)}",
            )
        return tuple(epochs)

    async def collect_proof_epoch(
        self, epoch_index: int, outcomes: Sequence[D2DispatchOutcome]
    ) -> D2ProofEpoch:
        open_orders = await self._client.get_all_open_orders()
        balances: list[dict[str, str]] = []
        for asset in [order.asset for order in self._orders] + [D2_QUOTE_ASSET]:
            balance = await self._client.get_asset_balance(asset=asset)
            balances.append(
                {
                    "asset": balance.asset,
                    "free": format(balance.free, "f"),
                    "locked": format(balance.locked, "f"),
                }
            )
        ledger_states: dict[str, str] = {}
        for outcome in outcomes:
            row = await self._ledger.get_by_client_order_id(outcome.client_order_id)
            ledger_states[outcome.client_order_id] = (
                "" if row is None else str(row.lifecycle_state)
            )
        return D2ProofEpoch(
            epoch_index=epoch_index,
            observed_at_utc=self._now_fn().isoformat(),
            open_orders=tuple(
                {
                    "symbol": entry.symbol,
                    "side": entry.side,
                    "client_order_id": entry.client_order_id,
                    "qty": format(entry.qty, "f"),
                    "status": entry.status,
                }
                for entry in open_orders.orders
            ),
            balances=tuple(balances),
            ledger_states=ledger_states,
        )

    def _evidence_metadata(self, op: D2PlannedOperation) -> dict[str, Any]:
        return {
            "writer": WRITER_NAME,
            "d2_exception_id": D2_EXCEPTION_ID,
            "remediation_id": D2_REMEDIATION_ID,
            "pre_snapshot_hash": D2_PRE_SNAPSHOT_HASH,
            "snapshot_seal_sha256": D2_SNAPSHOT_SEAL_SHA256,
            "binding_payload_sha256": self._authority.payload_sha256,
            "credential_fingerprint": self._authority.credential_fingerprint,
            "operation_id": op.operation_id,
            "canary_or_strategy_use": "forbidden",
        }


__all__ = [
    "D2_ALLOWED_OPERATION_IDS",
    "D2_BOUND_ORDERS",
    "D2_CLIENT_ORDER_ID_PREFIX",
    "D2_CREDENTIAL_FINGERPRINT",
    "D2_DISPATCH_AUTHORIZED_DIGESTS",
    "D2_EXCEPTION_ID",
    "D2_EXECUTION_AUTHORITY_KIND",
    "D2_EXECUTION_AUTHORITY_SCHEMA_VERSION",
    "D2_EXECUTION_AUTHORITY_SCOPE",
    "D2_KNOWN_SEALED_PAYLOADS",
    "D2_LANE_ID",
    "D2_ORDER_TYPE",
    "D2_PRE_SNAPSHOT_HASH",
    "D2_PRODUCT",
    "D2_QUOTE_ASSET",
    "D2_R8_ARTIFACT_MANIFEST_SHA256",
    "D2_R8_BINDING_PAYLOAD_SHA256",
    "D2_R8_EXECUTION_AUTHORITY_PATH",
    "D2_R8_EXECUTION_AUTHORITY_SHA256",
    "D2_R8_SEAL_TIMESTAMP_UTC",
    "D2_REMEDIATION_ENABLED_ENV",
    "D2_REMEDIATION_ID",
    "D2_SIDE",
    "D2_SNAPSHOT_SEAL_SHA256",
    "D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH",
    "D2_TIME_IN_FORCE",
    "D2_VENUE",
    "D2_VENUE_HOST",
    "D2AccountTruth",
    "D2AccountTruthDrift",
    "D2BlindRetryRefused",
    "D2BoundOrder",
    "D2ClaimNotDurable",
    "D2DispatchNotAuthorized",
    "D2DispatchOutcome",
    "D2DryRunReport",
    "D2ExecutionReport",
    "D2LeaseNotHeld",
    "D2LedgerRequired",
    "D2OutcomeUnknown",
    "D2PlannedOperation",
    "D2PriorAttemptUnresolved",
    "D2ProofEpoch",
    "D2ReasonCode",
    "D2RemediationDisabled",
    "D2RemediationError",
    "D2RemediationSingleWriter",
    "D2SealBindingMismatch",
    "D2UnauthorizedOperation",
    "SealedAuthority",
    "SealedPayloadRecord",
    "WRITER_NAME",
    "acquire_d2_lease",
    "assert_registry_credential_fingerprint",
    "assert_writer_freeze",
    "bind_sealed_orders",
    "broker_identifier_problem",
    "d2_advisory_keyset",
    "d2_physical_account_id",
    "d2_physical_account_scope",
    "d2_remediation_enabled",
    "load_sealed_authority",
]
