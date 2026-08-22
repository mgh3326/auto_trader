"""ROB-1272 (J7) — cross-lane mock/paper/demo observation read model.

Read-only by construction:

* no database write, no external file write, no broker network call;
* no module-level import of a ledger service, broker adapter, journal writer,
  scheduler, task or flow — the orch-stamped reader symbols are resolved
  lazily through an exact allowlist and only the named read method is called;
* no lane row is ever dropped, and no anomaly, hold or unlinked record is ever
  silently discarded.

The source bindings, the lane→source map and the predecessor chain below are
transcribed from the orch-stamped manifest and bindsheet. This module does not
select a source, a writer, a cadence, a cap or a canary.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

from app.schemas.execution_contracts import EvidenceTier
from app.schemas.mock_auto_read_model import (
    SCHEMA_VERSION,
    AncestorUnknown,
    AnomalyEntry,
    EvidenceClass,
    EvidenceRef,
    EvidenceSourceBinding,
    HoldEntry,
    LaneCoverageRow,
    LifecycleObservationRow,
    LifecycleStage,
    ManifestRef,
    MockAutoReadModelResponse,
    PredecessorRecord,
    QuoteCurrency,
    ReadModelNotes,
    UnlinkedEvidenceEntry,
    canonical_evidence_refs,
    derive_observation_id,
)
from app.services.mock_lane_registry import (
    CANONICAL_LANE_IDS,
    CANONICAL_LANE_REGISTRY,
)

# Lane identity is taken from the canonical registry rather than re-spelled
# here. The unpack is itself a drift detector: it fails loudly if the canonical
# set ever stops being exactly these twelve rows in this order — which is the
# order of the manifest §B coverage table (rows 1-12).
#
# Addressing lanes and the crypto demo-ledger source through the registry also
# keeps this read-only observation module from becoming a second venue code
# location under ROB-285: the venue literals stay in mock_lane_registry, which
# already owns them. The values resolved here are pinned by
# tests/services/test_mock_auto_read_model.py.
(
    _KR_KIS,
    _KR_KIWOOM,
    _US_KIS,
    _US_KIWOOM,
    _US_ALPACA_DEFAULT,
    _US_ALPACA_LAB,
    _CRYPTO_SPOT_DEMO_CANONICAL,
    _CRYPTO_SPOT_DEMO_SIDECAR,
    _CRYPTO_ALPACA_DEFAULT,
    _CRYPTO_ALPACA_CLEAN,
    _CRYPTO_UPBIT_SHADOW,
    _CRYPTO_FUTURES_DEMO,
) = CANONICAL_LANE_IDS

_CRYPTO_DEMO_BROKER: Final[str] = _CRYPTO_SPOT_DEMO_CANONICAL.split(".")[1]
_CRYPTO_DEMO_LEDGER_SOURCE_ID: Final[str] = f"{_CRYPTO_DEMO_BROKER}_demo_ledger"
_CRYPTO_DEMO_LEDGER_LOCATOR: Final[str] = f"{_CRYPTO_DEMO_BROKER}_demo_order_ledger"
_CRYPTO_DEMO_LEDGER_READER: Final[str] = (
    f"app.mcp_server.tooling.{_CRYPTO_DEMO_BROKER}_demo_ledger_status_read"
    f".{_CRYPTO_DEMO_BROKER}_demo_ledger_status"
)

# ---------------------------------------------------------------------------
# Orch-stamped binding identity (J7 brief §F). Not worker-selected.
# ---------------------------------------------------------------------------

J7_SOURCE_BINDING_MANIFEST: Final[str] = (
    "~/work/herdr-inbox/manifest-j7-source-binding-20260822.md"
)
J7_SOURCE_BINDING_MANIFEST_SHA256: Final[str] = (
    "6a71c1e53bc0aeb6790e2513b8aba85ae05e77e26101fefdeb199163d6cc732c"
)

J7_PREDECESSORS: Final[tuple[PredecessorRecord, ...]] = (
    PredecessorRecord(
        job="J2A",
        merge_sha="e057941425d2ea7d35a36ebf6074a6c70eba3013",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-codexmock-j2a-premerge-audit-20260816.md"
        ),
        verifier_report_sha256=(
            "2fff268eb1be89cb7aa200544bd9d30e706bce2b86efa0e322eac43d6c29aefe"
        ),
    ),
    PredecessorRecord(
        job="J2B",
        merge_sha="094ab2d59d6f2bf5fc3df4efa43bb5d412221ffd",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-codexmock-j2b-g1g2-static-review-20260816.md"
        ),
        verifier_report_sha256=(
            "3011d016ce3d4639844a2380dfbf3bbb796e4ae1b9e22414d445286f7776b635"
        ),
    ),
    PredecessorRecord(
        job="J3A",
        merge_sha="03beecc5f53e636c352ddf0527aa3d98ddc7bd61",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j3a-identity19-review-a-20260816.md"
        ),
        verifier_report_sha256=(
            "31f91bd00341bf2c40e5ab764e4d706284b62fa6c73534ee7a5f9121893b6d3f"
        ),
    ),
    PredecessorRecord(
        job="J3B",
        merge_sha="9dedd3b86aed1f74a573ed918d2a431caa3eb2aa",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j3b-rob1263-verify-round6-20260817.md"
        ),
        verifier_report_sha256=(
            "8184be1607b5029b3be159a2b1cc9946a6c61cc672abf702fca4645fbc463b3a"
        ),
    ),
    PredecessorRecord(
        job="J3C",
        merge_sha="5c55aeed0a2fa07e83303f00e3b3139cbd74175e",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j3c-rob1264-verify-round4-20260817.md"
        ),
        verifier_report_sha256=(
            "3ce25431e17b3b2b9dc100583996fb13614336afe69a8f9c3e76ad71e176463f"
        ),
    ),
    PredecessorRecord(
        job="J5A",
        merge_sha="ddf4895ece2ca9dff8daf1a04fa7d6143f43c899",
        verifier_report_path="~/work/herdr-inbox/answer-orchmock-j5a-verify-20260818.md",
        verifier_report_sha256=(
            "1d11556c0d8df0f3629b0d185e1840bb120ed78682ff8b8840f203f213044cf2"
        ),
    ),
    PredecessorRecord(
        job="J5B",
        merge_sha="f16bbdfae016664b64f2b13423185329b87e893e",
        verifier_report_path="~/work/herdr-inbox/answer-orchmock-j5b-verify-20260821.md",
        verifier_report_sha256=(
            "7168edd34f0cfc65c18bf698b2421e98dd1f62d818c74f53c2d11ac97c3f1e18"
        ),
    ),
    PredecessorRecord(
        job="J5C",
        merge_sha="6db485f2adfc6fb1861c61285cb4e7c2255c0042",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j5c-verify-r3-20260821.md"
        ),
        verifier_report_sha256=(
            "8aa69b5d8f87b9c9ad38820fab1d5b529e68295d9768631754c6fd74ff7c239d"
        ),
    ),
    PredecessorRecord(
        job="J6A",
        merge_sha="a2faafba288b322600c71d1fec26a7878df4f41f",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j6a-rob1269-verify-20260817.md"
        ),
        verifier_report_sha256=(
            "040f7d530e148e49ce308647d8c51730490a9d31c7be1b49ef46a2f8fd61fd3b"
        ),
    ),
    PredecessorRecord(
        job="J6B",
        merge_sha="d7627f5f1f9d313586e7b0e875c735e217d9aaa2",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j6b-rob1270-verify-r2-20260817.md"
        ),
        verifier_report_sha256=(
            "ef7555cf7da5c16c43d9a30f52fce6a7306b71c77b7f93365f516d52adeb9e15"
        ),
    ),
    PredecessorRecord(
        job="J6C",
        merge_sha="797874b089c7f4386b4bf368125743bcf15ea515",
        verifier_report_path=(
            "~/work/herdr-inbox/answer-orchmock-j6c-rob1271-verify-r3-20260817.md"
        ),
        verifier_report_sha256=(
            "3eaceefae64bcb7fc391ca46d74216d7c5eb0fb962136b498e8d37bf4656ec5a"
        ),
    ),
)

#: The bindsheet records J3A's canonical verifier output as a *pair*. The
#: schema binds one path per predecessor, so review-a (the 163-line detailed
#: report) is the bound record and review-b is carried here so that its verdict
#: is not erased. Both concluded ``SAFE_TO_SPAWN_J4=YES`` with
#: ``NEW_BLOCKER=0``.
J3A_REVIEW_B_COMPANION: Final[Mapping[str, str]] = {
    "path": ("~/work/herdr-inbox/answer-orchmock-j3a-identity19-review-b-20260816.md"),
    "sha256": "d6cac47bc82150e67ac2860d1e98ab41de248a5b53364ecdcb09f9f3cd3303f3",
    "code": "j3a_verifier_report_pair_second_member",
}

#: Ancestors that were merged with an unresolved axis. J7 inherits the UNKNOWN
#: and surfaces it rather than swallowing it.
ANCESTOR_UNKNOWNS: Final[tuple[AncestorUnknown, ...]] = (
    AncestorUnknown(
        job="J3A",
        axis="C5",
        verifier_report_path=J7_PREDECESSORS[2].verifier_report_path,
        verifier_report_sha256=J7_PREDECESSORS[2].verifier_report_sha256,
        disposition="merged with C5=UNKNOWN; J7 downgrades affected evidence_tier",
    ),
    AncestorUnknown(
        job="J6C",
        axis="C5",
        verifier_report_path=J7_PREDECESSORS[10].verifier_report_path,
        verifier_report_sha256=J7_PREDECESSORS[10].verifier_report_sha256,
        disposition=(
            "merged with VERIFIED=NO, C5=UNKNOWN, NEEDS_OPERATOR=YES; "
            "J7 downgrades affected evidence_tier"
        ),
    ),
)

ANCESTOR_UNKNOWN_ANOMALY_PREFIX: Final[str] = "ancestor_c5_unknown"
#: J3A established the canonical lane identity every row depends on, so its
#: UNKNOWN taints all twelve lanes. J6C owns exactly the Upbit shadow and
#: USD-M futures lanes (downstream preflight §4 "J6C").
J6C_OWNED_LANE_IDS: Final[frozenset[str]] = frozenset(
    {_CRYPTO_UPBIT_SHADOW, _CRYPTO_FUTURES_DEMO}
)

_PREDECESSOR_BY_JOB: Final[Mapping[str, PredecessorRecord]] = {
    record.job: record for record in J7_PREDECESSORS
}

_REDACTION_CONTRACT: Final[str] = (
    "no account identifier, credential, secret or DSN is read into or returned "
    "from this model; native keys are broker/order identifiers only"
)

# ---------------------------------------------------------------------------
# EvidenceSourceBinding table — transcribed from the manifest, §A.
# ---------------------------------------------------------------------------


def _binding(
    *,
    source_id: str,
    evidence_class: EvidenceClass,
    reader: str,
    locator: str,
    discriminator: str,
    predecessor_job: str,
    read_scope_note: str,
) -> EvidenceSourceBinding:
    predecessor = _PREDECESSOR_BY_JOB[predecessor_job]
    return EvidenceSourceBinding(
        source_id=source_id,
        evidence_class=evidence_class,
        read_only_reader_symbol=reader,
        logical_locator=locator,
        format_version="UNKNOWN(미정의)",
        lane_account_discriminator=discriminator,
        redaction_contract=_REDACTION_CONTRACT,
        read_scope_note=read_scope_note,
        predecessor_job=predecessor.job,
        predecessor_merge_sha=predecessor.merge_sha,
        predecessor_verifier_report_path=predecessor.verifier_report_path,
        predecessor_verifier_report_sha256=predecessor.verifier_report_sha256,
    )


#: The manifest binds this reader but the investigator could not confirm an
#: independently persisted readback artifact, so no reader symbol exists.
UNRESOLVED_READER_SYMBOL: Final[str] = "UNKNOWN(reader 미확정)"

EVIDENCE_SOURCE_BINDINGS: Final[tuple[EvidenceSourceBinding, ...]] = (
    _binding(
        source_id="alpaca_paper_ledger",
        evidence_class=EvidenceClass.DB_LEDGER,
        reader="app.services.alpaca_paper_ledger_service.AlpacaPaperLedgerService.list_recent",
        locator="review.alpaca_paper_order_ledger",
        discriminator="account_mode",
        predecessor_job="J5B",
        read_scope_note="bounded newest-first read; not a full-table scan",
    ),
    _binding(
        source_id=_CRYPTO_DEMO_LEDGER_SOURCE_ID,
        evidence_class=EvidenceClass.DB_LEDGER,
        reader=_CRYPTO_DEMO_LEDGER_READER,
        locator=_CRYPTO_DEMO_LEDGER_LOCATOR,
        discriminator="product + venue_host",
        predecessor_job="J6B",
        read_scope_note="bounded recent-window status read; not a full-table scan",
    ),
    _binding(
        source_id="kis_mock_ledger",
        evidence_class=EvidenceClass.DB_LEDGER,
        reader=(
            "app.services.kis_mock_lifecycle_service.KISMockLifecycleService"
            ".list_open_orders"
        ),
        locator="review.kis_mock_order_ledger",
        discriminator="account_mode='kis_mock' (KR/US not separable)",
        predecessor_job="J3B",
        read_scope_note="open lifecycle states only; terminal rows are out of scope",
    ),
    _binding(
        source_id="kiwoom_kr_native_readback",
        evidence_class=EvidenceClass.BROKER_READBACK,
        reader=UNRESOLVED_READER_SYMBOL,
        locator="<OBS_DIR>/kr.kiwoom.mock/ordering-events.jsonl#kt00007.raw_row",
        discriminator="journal path + broker_order_id",
        predecessor_job="J3C",
        read_scope_note=(
            "no independently persisted artifact; reader unresolved, so this "
            "source always yields an anomaly and never a lifecycle row"
        ),
    ),
    _binding(
        source_id="kiwoom_kr_ordering_events",
        evidence_class=EvidenceClass.FILE_JOURNAL,
        reader="scripts.b0x.kr.kiwoom_ordering.OrderingEventJournal.read_all",
        locator="<OBS_DIR>/kr.kiwoom.mock/ordering-events.jsonl",
        discriminator="journal path segment + broker_order_id",
        predecessor_job="J3C",
        read_scope_note="append-only journal read in full; corrupt rows fail closed",
    ),
    _binding(
        source_id="kiwoom_kr_own_orders",
        evidence_class=EvidenceClass.FILE_JOURNAL,
        reader="scripts.b0x.kr.kiwoom_attribution.OwnOrderJournal.read_all",
        locator="<OBS_DIR>/kr.kiwoom.mock/own-orders.jsonl",
        discriminator="journal path segment + correlation_id/order_no",
        predecessor_job="J3C",
        read_scope_note="append-only journal read in full; corrupt rows fail closed",
    ),
)

_BINDING_BY_SOURCE_ID: Final[Mapping[str, EvidenceSourceBinding]] = {
    binding.source_id: binding for binding in EVIDENCE_SOURCE_BINDINGS
}

#: Only these dotted symbols may ever be resolved. Anything else fails closed.
READER_SYMBOL_ALLOWLIST: Final[frozenset[str]] = frozenset(
    binding.read_only_reader_symbol
    for binding in EVIDENCE_SOURCE_BINDINGS
    if binding.read_only_reader_symbol != UNRESOLVED_READER_SYMBOL
)

# ---------------------------------------------------------------------------
# LaneCoverageRow bindings — transcribed from the manifest, §B.
# ---------------------------------------------------------------------------

LANE_SOURCE_IDS: Final[Mapping[str, tuple[str, ...]]] = {
    _KR_KIS: ("kis_mock_ledger",),
    _KR_KIWOOM: (
        "kiwoom_kr_native_readback",
        "kiwoom_kr_ordering_events",
        "kiwoom_kr_own_orders",
    ),
    _US_KIS: (),
    _US_KIWOOM: (),
    _US_ALPACA_DEFAULT: ("alpaca_paper_ledger",),
    _US_ALPACA_LAB: ("alpaca_paper_ledger",),
    _CRYPTO_SPOT_DEMO_CANONICAL: (_CRYPTO_DEMO_LEDGER_SOURCE_ID,),
    _CRYPTO_SPOT_DEMO_SIDECAR: (),
    _CRYPTO_ALPACA_DEFAULT: ("alpaca_paper_ledger",),
    _CRYPTO_ALPACA_CLEAN: ("alpaca_paper_ledger",),
    _CRYPTO_UPBIT_SHADOW: (),
    _CRYPTO_FUTURES_DEMO: (_CRYPTO_DEMO_LEDGER_SOURCE_ID,),
}

#: Structural reasons a lane can produce no lifecycle evidence at all. These
#: are manifest facts, not worker judgements.
LANE_STRUCTURAL_NO_EVIDENCE_REASON: Final[Mapping[str, str]] = {
    _US_KIS: "kis_mock_order_ledger is not lane-separable for us.kis.mock",
    _US_KIWOOM: (
        "no Kiwoom US order ledger or persisted lane-native readback artifact"
    ),
    _CRYPTO_SPOT_DEMO_SIDECAR: (
        "shared spot product discriminator cannot attribute a row to b0x_sidecar"
    ),
    _CRYPTO_UPBIT_SHADOW: (
        "shadow-only lane has no broker I/O or persisted lifecycle evidence surface"
    ),
}

#: Manifest-stamped anomalies that are true of the binding itself, regardless
#: of what a read returns.
LANE_STATIC_ANOMALY_CODES: Final[Mapping[str, tuple[str, ...]]] = {
    _KR_KIS: ("lane_discriminator_insufficient:account_mode_shared_with_us_kis_mock",),
    _KR_KIWOOM: ("readback_not_independently_persisted",),
    _US_ALPACA_DEFAULT: (
        "lane_discriminator_insufficient:account_mode_shared_with_crypto_alpaca_default",
    ),
    _CRYPTO_SPOT_DEMO_CANONICAL: (
        "lane_discriminator_insufficient:product_spot_shared_with_b0x_sidecar",
    ),
    _CRYPTO_ALPACA_DEFAULT: (
        "lane_discriminator_insufficient:account_mode_shared_with_us_alpaca_default",
        "asset_class_predicate_not_promoted",
    ),
}

#: Lanes whose evidence is synthetic rather than broker-native.
SYNTHETIC_LANE_IDS: Final[frozenset[str]] = frozenset({_CRYPTO_UPBIT_SHADOW})

#: The J7 brief pins these two meanings; they travel with every response so a
#: consumer cannot re-read ``None`` as ``disabled`` or ``role`` as authority.
ROLE_SEMANTICS_NOTE: Final[str] = (
    "role is the registry purpose value only; it is not execution authority"
)
SCHEDULER_OWNER_ABSENT_NOTE: Final[str] = (
    "None (owner absent; mutation-ineligible; downstream bind authority 없음)"
)
LINEAGE_REQUIREMENT_NOTE: Final[str] = (
    "decision_intent_id, execution_plan_id and order_attempt_id are preserved "
    "separately; native evidence without J2B lineage is reported as unlinked, "
    "never converted into a lifecycle row"
)
AGGREGATION_BOUNDARY_NOTE: Final[str] = (
    "synthetic and native evidence are never summed, and KRW/USD/USDT are "
    "never converted or compared"
)

# ---------------------------------------------------------------------------
# Fail-closed journal reading
# ---------------------------------------------------------------------------


class JournalReadRejected(RuntimeError):
    """A journal could not be read safely. Never collapsed into 'no rows'."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


JOURNAL_ROOT_UNSET: Final[str] = "journal_root_unset"
JOURNAL_ROOT_NOT_A_DIRECTORY: Final[str] = "journal_root_not_a_directory"
JOURNAL_PATH_ESCAPES_ROOT: Final[str] = "journal_path_escapes_root"
JOURNAL_PATH_IS_SYMLINK: Final[str] = "journal_path_is_symlink"
JOURNAL_PATH_NOT_REGULAR_FILE: Final[str] = "journal_path_not_regular_file"
JOURNAL_LOCATOR_ABSENT: Final[str] = "journal_locator_absent"
JOURNAL_ROW_CORRUPT: Final[str] = "journal_row_corrupt"
READER_SYMBOL_UNRESOLVED: Final[str] = "reader_symbol_unresolved"
READER_SYMBOL_NOT_ALLOWLISTED: Final[str] = "reader_symbol_not_allowlisted"
SOURCE_READ_FAILED: Final[str] = "source_read_failed"
EVIDENCE_LINEAGE_ABSENT: Final[str] = "evidence_lineage_absent"
STAGE_EVIDENCE_INSUFFICIENT: Final[str] = "stage_evidence_insufficient"


def resolve_journal_path(
    *, root: Path | None, lane_segment: str, filename: str
) -> Path:
    """Resolve one allowlisted journal path or fail closed.

    Path traversal, symlink escape, a missing root and a non-regular file are
    each a distinct reason code; none of them is reported as an empty journal.
    """

    if root is None:
        raise JournalReadRejected(JOURNAL_ROOT_UNSET, "no allowlisted journal root")
    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.is_dir():
        raise JournalReadRejected(JOURNAL_ROOT_NOT_A_DIRECTORY, str(resolved_root))
    if "/" in lane_segment or "\\" in lane_segment or lane_segment in {"", ".", ".."}:
        raise JournalReadRejected(JOURNAL_PATH_ESCAPES_ROOT, lane_segment)
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise JournalReadRejected(JOURNAL_PATH_ESCAPES_ROOT, filename)

    candidate = resolved_root / lane_segment / filename
    if candidate.is_symlink() or (
        candidate.parent.exists() and candidate.parent.is_symlink()
    ):
        raise JournalReadRejected(JOURNAL_PATH_IS_SYMLINK, str(candidate))
    if not candidate.exists():
        raise JournalReadRejected(JOURNAL_LOCATOR_ABSENT, str(candidate))
    real = candidate.resolve()
    if resolved_root not in real.parents:
        raise JournalReadRejected(JOURNAL_PATH_ESCAPES_ROOT, str(real))
    if not real.is_file():
        raise JournalReadRejected(JOURNAL_PATH_NOT_REGULAR_FILE, str(real))
    return real


def read_jsonl_fail_closed(path: Path) -> tuple[dict[str, Any], ...]:
    """Read a JSONL evidence file. A corrupt row is never skipped."""

    rows: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception as exc:  # noqa: BLE001 — any parse failure is corruption
            raise JournalReadRejected(
                JOURNAL_ROW_CORRUPT, f"{path}:{number}:{type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise JournalReadRejected(
                JOURNAL_ROW_CORRUPT, f"{path}:{number}:row is not an object"
            )
        rows.append(payload)
    return tuple(rows)


def resolve_reader_symbol(symbol: str) -> Any:
    """Lazily resolve one allowlisted dotted reader symbol.

    Resolution is deliberately late and allowlisted: no J7 module holds a
    static import edge to a ledger service, journal writer or broker adapter.
    """

    if symbol == UNRESOLVED_READER_SYMBOL:
        raise JournalReadRejected(READER_SYMBOL_UNRESOLVED, symbol)
    if symbol not in READER_SYMBOL_ALLOWLIST:
        raise JournalReadRejected(READER_SYMBOL_NOT_ALLOWLISTED, symbol)
    parts = symbol.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:split])
        attribute_path = parts[split:]
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            continue
        target: Any = module
        for attribute in attribute_path:
            target = getattr(target, attribute)
        return target
    raise JournalReadRejected(READER_SYMBOL_NOT_ALLOWLISTED, symbol)


# ---------------------------------------------------------------------------
# Evidence records and ports
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawEvidenceRecord:
    """One normalized evidence record produced by a read-only source port."""

    source_id: str
    evidence_class: EvidenceClass
    native_key: str
    as_of: datetime
    native_status: str
    venue_basis: str
    observed_at: datetime
    decision_intent_id: str | None = None
    execution_plan_id: str | None = None
    order_attempt_id: str | None = None
    cycle_id: str | None = None
    idempotency_key: str | None = None
    broker_ack: bool = False
    fill_evidence: bool = False
    filled_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    terminal_outcome: str | None = None
    position_convergence: bool = False
    anomaly_codes: tuple[str, ...] = ()
    on_hold: bool = False
    hold_reason_codes: tuple[str, ...] = ()

    @property
    def has_lineage(self) -> bool:
        return bool(self.decision_intent_id and self.decision_intent_id.strip())

    def evidence_ref(self) -> EvidenceRef:
        return EvidenceRef(
            evidence_class=self.evidence_class,
            source_id=self.source_id,
            native_key=self.native_key,
            as_of=self.as_of,
        )


@dataclass(frozen=True, slots=True)
class SourceReadResult:
    """What one source returned, including why it could not be read."""

    source_id: str
    records: tuple[RawEvidenceRecord, ...] = ()
    anomaly_codes: tuple[str, ...] = ()
    unreadable_reason: str | None = None


class EvidenceSourcePort(Protocol):
    """Read-only port. Implementations must never write or call a broker."""

    async def read(self, *, lane_id: str, source_id: str) -> SourceReadResult: ...


# ---------------------------------------------------------------------------
# Read-only production ports
# ---------------------------------------------------------------------------

#: The manifest stamps the Kiwoom journal locator under the canonical lane id.
#: The in-repo writer (``scripts/b0x/kr/kiwoom.py`` ``LANE``) writes under this
#: segment instead. J7 reads the *stamped* locator and reports the difference;
#: it does not silently substitute a source the manifest did not bind.
KIWOOM_MANIFEST_JOURNAL_SEGMENT: Final[str] = "kr.kiwoom.mock"
KIWOOM_REPO_WRITER_JOURNAL_SEGMENT: Final[str] = "kiwoom_mock"
MANIFEST_LOCATOR_SEGMENT_DIFFERS: Final[str] = (
    "manifest_locator_segment_differs_from_repo_writer"
)

#: Exact per-lane discriminator values, transcribed from the manifest.
LANE_SOURCE_DISCRIMINATORS: Final[Mapping[tuple[str, str], Mapping[str, str]]] = {
    (_KR_KIS, "kis_mock_ledger"): {"account_mode": "kis_mock"},
    (_US_ALPACA_DEFAULT, "alpaca_paper_ledger"): {"account_mode": "alpaca_paper"},
    (_US_ALPACA_LAB, "alpaca_paper_ledger"): {"account_mode": "alpaca_paper_lab"},
    (_CRYPTO_ALPACA_DEFAULT, "alpaca_paper_ledger"): {"account_mode": "alpaca_paper"},
    (_CRYPTO_ALPACA_CLEAN, "alpaca_paper_ledger"): {
        "account_mode": "alpaca_paper_crypto"
    },
    (_CRYPTO_SPOT_DEMO_CANONICAL, _CRYPTO_DEMO_LEDGER_SOURCE_ID): {"product": "spot"},
    (_CRYPTO_FUTURES_DEMO, _CRYPTO_DEMO_LEDGER_SOURCE_ID): {"product": "usdm_futures"},
}

_DEFAULT_READ_LIMIT: Final[int] = 200


def resolve_reader_callable(symbol: str) -> tuple[Any, str]:
    """Return ``(owner, attribute)`` for one allowlisted reader symbol."""

    if symbol == UNRESOLVED_READER_SYMBOL:
        raise JournalReadRejected(READER_SYMBOL_UNRESOLVED, symbol)
    if symbol not in READER_SYMBOL_ALLOWLIST:
        raise JournalReadRejected(READER_SYMBOL_NOT_ALLOWLISTED, symbol)
    owner_symbol, _, attribute = symbol.rpartition(".")
    parts = owner_symbol.split(".")
    for split in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        owner: Any = module
        for name in parts[split:]:
            owner = getattr(owner, name)
        return owner, attribute
    raise JournalReadRejected(READER_SYMBOL_NOT_ALLOWLISTED, symbol)


def default_journal_root() -> Path | None:
    """The in-repo observation root, or ``None`` when it cannot be resolved."""

    try:
        module = importlib.import_module("scripts.b0x.ledger")
    except Exception:  # noqa: BLE001 — an unresolvable root is reported, not raised
        return None
    root = getattr(module, "DEFAULT_OBSERVATION_DIR", None)
    return Path(root) if root is not None else None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — an unparseable quantity is not a quantity
        return None


def _as_datetime(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


@dataclass(frozen=True, slots=True)
class DbLedgerSourcePort:
    """Bounded, read-only read of one stamped DB ledger source.

    The reader symbol is resolved lazily through the allowlist, so no J7
    module holds a static import edge to a ledger service. Only the stamped
    read method is ever called.
    """

    source_id: str
    session: Any = None
    limit: int = _DEFAULT_READ_LIMIT
    reader_override: Any = None

    async def read(self, *, lane_id: str, source_id: str) -> SourceReadResult:
        binding = _BINDING_BY_SOURCE_ID[source_id]
        discriminator = LANE_SOURCE_DISCRIMINATORS.get((lane_id, source_id), {})
        try:
            rows = await self._fetch(binding, discriminator)
        except JournalReadRejected as exc:
            return SourceReadResult(source_id=source_id, unreadable_reason=exc.code)
        except Exception as exc:  # noqa: BLE001 — a failed read is reported, not hidden
            return SourceReadResult(
                source_id=source_id,
                unreadable_reason=f"{SOURCE_READ_FAILED}:{type(exc).__name__}",
            )
        now = utc_now()
        records = tuple(
            _record_from_ledger_row(
                row=row,
                source_id=source_id,
                evidence_class=binding.evidence_class,
                fallback_time=now,
            )
            for row in rows
        )
        return SourceReadResult(source_id=source_id, records=records)

    async def _fetch(
        self, binding: EvidenceSourceBinding, discriminator: Mapping[str, str]
    ) -> Sequence[Any]:
        if self.reader_override is not None:
            return await self.reader_override(
                limit=self.limit, discriminator=dict(discriminator)
            )
        owner, attribute = resolve_reader_callable(binding.read_only_reader_symbol)
        if isinstance(owner, type):
            if "account_mode" in discriminator:
                instance = owner(self.session, discriminator["account_mode"])
            else:
                instance = owner(self.session)
            return await getattr(instance, attribute)(limit=self.limit)
        payload = await getattr(owner, attribute)(recent_limit=self.limit)
        rows = payload.get("recent", []) if isinstance(payload, Mapping) else []
        product = discriminator.get("product")
        if product is None:
            return list(rows)
        return [row for row in rows if _row_value(row, "product") == product]


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _record_from_ledger_row(
    *,
    row: Any,
    source_id: str,
    evidence_class: EvidenceClass,
    fallback_time: datetime,
) -> RawEvidenceRecord:
    """Map one native ledger row without inventing lineage it does not carry."""

    native_key = str(
        _row_value(row, "client_order_id")
        or _row_value(row, "order_no")
        or _row_value(row, "id")
        or "unknown"
    )
    as_of = _as_datetime(
        _row_value(row, "updated_at")
        or _row_value(row, "created_at")
        or _row_value(row, "trade_date"),
        fallback=fallback_time,
    )
    return RawEvidenceRecord(
        source_id=source_id,
        evidence_class=evidence_class,
        native_key=f"{source_id}:{native_key}",
        as_of=as_of,
        native_status=str(_row_value(row, "lifecycle_state") or "unknown"),
        venue_basis=source_id,
        observed_at=fallback_time,
        decision_intent_id=_optional_str(_row_value(row, "decision_intent_id")),
        execution_plan_id=_optional_str(_row_value(row, "execution_plan_id")),
        order_attempt_id=_optional_str(_row_value(row, "order_attempt_id")),
        cycle_id=_optional_str(_row_value(row, "cycle_id")),
        idempotency_key=_optional_str(_row_value(row, "idempotency_key")),
        filled_quantity=_as_decimal(_row_value(row, "filled_quantity")),
        remaining_quantity=_as_decimal(_row_value(row, "remaining_quantity")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


@dataclass(frozen=True, slots=True)
class JournalSourcePort:
    """Read-only read of one allowlisted append-only journal."""

    source_id: str
    filename: str
    root: Path | None = None
    lane_segment: str = KIWOOM_MANIFEST_JOURNAL_SEGMENT

    async def read(self, *, lane_id: str, source_id: str) -> SourceReadResult:
        del lane_id
        binding = _BINDING_BY_SOURCE_ID[source_id]
        anomaly_codes: tuple[str, ...] = ()
        if self.lane_segment != KIWOOM_REPO_WRITER_JOURNAL_SEGMENT:
            anomaly_codes = (MANIFEST_LOCATOR_SEGMENT_DIFFERS,)
        try:
            path = resolve_journal_path(
                root=self.root, lane_segment=self.lane_segment, filename=self.filename
            )
            rows = read_jsonl_fail_closed(path)
        except JournalReadRejected as exc:
            return SourceReadResult(
                source_id=source_id,
                anomaly_codes=anomaly_codes,
                unreadable_reason=exc.code,
            )
        except OSError as exc:
            return SourceReadResult(
                source_id=source_id,
                anomaly_codes=anomaly_codes,
                unreadable_reason=f"{SOURCE_READ_FAILED}:{type(exc).__name__}",
            )
        now = utc_now()
        records = tuple(
            _record_from_ledger_row(
                row=row,
                source_id=source_id,
                evidence_class=binding.evidence_class,
                fallback_time=now,
            )
            for row in rows
        )
        return SourceReadResult(
            source_id=source_id, records=records, anomaly_codes=anomaly_codes
        )


def build_default_ports(
    *, session: Any = None, journal_root: Path | None = None
) -> dict[str, EvidenceSourcePort]:
    """Wire the stamped read-only sources. Sources with no reader are omitted.

    ``kiwoom_kr_native_readback`` is deliberately absent: the manifest could
    not confirm an independently persisted artifact, so it has no reader and
    is always reported as an anomaly rather than read.
    """

    root = journal_root if journal_root is not None else default_journal_root()
    ports: dict[str, EvidenceSourcePort] = {
        "kis_mock_ledger": DbLedgerSourcePort(
            source_id="kis_mock_ledger", session=session
        ),
        "alpaca_paper_ledger": DbLedgerSourcePort(
            source_id="alpaca_paper_ledger", session=session
        ),
        _CRYPTO_DEMO_LEDGER_SOURCE_ID: DbLedgerSourcePort(
            source_id=_CRYPTO_DEMO_LEDGER_SOURCE_ID, session=session
        ),
        "kiwoom_kr_own_orders": JournalSourcePort(
            source_id="kiwoom_kr_own_orders",
            filename="own-orders.jsonl",
            root=root,
        ),
        "kiwoom_kr_ordering_events": JournalSourcePort(
            source_id="kiwoom_kr_ordering_events",
            filename="ordering-events.jsonl",
            root=root,
        ),
    }
    return ports


# ---------------------------------------------------------------------------
# Stage normalizer
# ---------------------------------------------------------------------------


def normalize_stage(record: RawEvidenceRecord) -> LifecycleStage:
    """Map one evidence record to a lifecycle stage, or refuse.

    ``acked`` never becomes ``filled``; ``filled`` never becomes
    ``reconciled`` without both a terminal outcome and convergence evidence.
    No stage is synthesized when the intermediate evidence is missing.
    """

    if record.terminal_outcome is not None and record.position_convergence:
        return LifecycleStage.RECONCILED
    if record.fill_evidence:
        if record.filled_quantity is None or record.filled_quantity <= 0:
            raise JournalReadRejected(
                STAGE_EVIDENCE_INSUFFICIENT,
                f"{record.native_key}: fill evidence without filled_quantity > 0",
            )
        return LifecycleStage.FILLED
    if record.broker_ack:
        return LifecycleStage.ACKED
    if record.terminal_outcome is not None and not record.position_convergence:
        # Terminal at the broker but not converged in the account: this is not
        # reconciled, and it is not a fill either.
        return LifecycleStage.ACKED
    return LifecycleStage.PLANNED


def _lifecycle_row(
    *,
    lane_id: str,
    record: RawEvidenceRecord,
    quote_currency: QuoteCurrency,
    synthetic: bool,
    inherited_anomalies: Sequence[str],
) -> LifecycleObservationRow:
    stage = normalize_stage(record)
    refs = canonical_evidence_refs([record.evidence_ref()])
    convergence = (
        canonical_evidence_refs([record.evidence_ref()])
        if stage is LifecycleStage.RECONCILED
        else ()
    )
    anomaly_codes = tuple(sorted({*record.anomaly_codes, *inherited_anomalies}))
    partial_fill = bool(
        record.remaining_quantity is not None and record.remaining_quantity > 0
    )
    tier = EvidenceTier.INFERENCE if anomaly_codes else EvidenceTier.FACT
    observation_id = derive_observation_id(
        lane_id=lane_id,
        decision_intent_id=str(record.decision_intent_id),
        execution_plan_id=record.execution_plan_id,
        order_attempt_id=record.order_attempt_id,
        cycle_id=record.cycle_id,
        idempotency_key=record.idempotency_key,
        stage=stage,
        evidence_refs=refs,
    )
    return LifecycleObservationRow(
        lane_id=lane_id,
        decision_intent_id=str(record.decision_intent_id),
        execution_plan_id=record.execution_plan_id,
        order_attempt_id=record.order_attempt_id,
        cycle_id=record.cycle_id,
        idempotency_key=record.idempotency_key,
        stage=stage,
        observation_id=observation_id,
        evidence_refs=refs,
        synthetic=synthetic,
        quote_currency=quote_currency,
        venue_basis=record.venue_basis,
        native_status=record.native_status,
        native_terminal_outcome=record.terminal_outcome,
        partial_fill=partial_fill,
        filled_quantity=record.filled_quantity,
        remaining_quantity=record.remaining_quantity,
        convergence_evidence_refs=convergence,
        anomaly_codes=anomaly_codes,
        on_hold=record.on_hold,
        hold_reason_codes=tuple(sorted(record.hold_reason_codes)),
        evidence_tier=tier,
        observed_at=record.observed_at,
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LaneAccumulator:
    lifecycle_rows: list[LifecycleObservationRow] = field(default_factory=list)
    anomalies: list[AnomalyEntry] = field(default_factory=list)
    unlinked: list[UnlinkedEvidenceEntry] = field(default_factory=list)


def _registry_row(lane_id: str) -> Any:
    for entry in CANONICAL_LANE_REGISTRY:
        if entry.lane_id == lane_id:
            return entry
    raise KeyError(lane_id)


def _inherited_anomaly_codes(lane_id: str) -> tuple[str, ...]:
    codes = [f"{ANCESTOR_UNKNOWN_ANOMALY_PREFIX}:j3a_rob1262"]
    if lane_id in J6C_OWNED_LANE_IDS:
        codes.append(f"{ANCESTOR_UNKNOWN_ANOMALY_PREFIX}:j6c_rob1271")
    return tuple(sorted(codes))


async def build_read_model(
    *,
    ports: Mapping[str, EvidenceSourcePort] | None = None,
    as_of: datetime,
) -> MockAutoReadModelResponse:
    """Build the twelve coverage rows and every observation they account for.

    ``ports`` maps ``source_id`` to a read-only port. A source with no port is
    reported as unreadable, never as an empty source.
    """

    resolved_ports: Mapping[str, EvidenceSourcePort] = ports or {}
    accumulators: dict[str, _LaneAccumulator] = {
        lane_id: _LaneAccumulator() for lane_id in CANONICAL_LANE_IDS
    }
    coverage_rows: list[LaneCoverageRow] = []

    for lane_id in CANONICAL_LANE_IDS:
        entry = _registry_row(lane_id)
        accumulator = accumulators[lane_id]
        source_ids = tuple(sorted(LANE_SOURCE_IDS[lane_id]))
        synthetic = lane_id in SYNTHETIC_LANE_IDS
        quote_currency: QuoteCurrency = entry.quote_currency
        inherited = _inherited_anomaly_codes(lane_id)
        anomaly_codes: set[str] = {
            *LANE_STATIC_ANOMALY_CODES.get(lane_id, ()),
            *inherited,
        }

        for source_id in source_ids:
            binding = _BINDING_BY_SOURCE_ID[source_id]
            if binding.read_only_reader_symbol == UNRESOLVED_READER_SYMBOL:
                code = f"{READER_SYMBOL_UNRESOLVED}:{source_id}"
                anomaly_codes.add(code)
                accumulator.anomalies.append(
                    AnomalyEntry(
                        lane_id=lane_id,
                        source_id=source_id,
                        code=code,
                        detail=binding.read_scope_note,
                    )
                )
                continue
            port = resolved_ports.get(source_id)
            if port is None:
                code = f"{SOURCE_READ_FAILED}:{source_id}:no_source_port_wired"
                anomaly_codes.add(code)
                accumulator.anomalies.append(
                    AnomalyEntry(
                        lane_id=lane_id,
                        source_id=source_id,
                        code=code,
                        detail="no read-only port wired for this source",
                    )
                )
                continue
            result = await port.read(lane_id=lane_id, source_id=source_id)
            for code in result.anomaly_codes:
                scoped = f"{code}:{source_id}"
                anomaly_codes.add(scoped)
                accumulator.anomalies.append(
                    AnomalyEntry(
                        lane_id=lane_id,
                        source_id=source_id,
                        code=scoped,
                        detail=code,
                    )
                )
            if result.unreadable_reason is not None:
                code = f"{SOURCE_READ_FAILED}:{source_id}:{result.unreadable_reason}"
                anomaly_codes.add(code)
                accumulator.anomalies.append(
                    AnomalyEntry(
                        lane_id=lane_id,
                        source_id=source_id,
                        code=code,
                        detail=result.unreadable_reason,
                    )
                )
                continue
            if not result.records:
                code = f"configured_source_returned_no_rows:{source_id}"
                anomaly_codes.add(code)
                accumulator.anomalies.append(
                    AnomalyEntry(
                        lane_id=lane_id,
                        source_id=source_id,
                        code=code,
                        detail="source is bound but returned zero records",
                    )
                )
                continue
            for record in result.records:
                if not record.has_lineage:
                    accumulator.unlinked.append(
                        UnlinkedEvidenceEntry(
                            lane_id=lane_id,
                            source_id=source_id,
                            evidence_class=record.evidence_class,
                            native_key=record.native_key,
                            reason=EVIDENCE_LINEAGE_ABSENT,
                        )
                    )
                    anomaly_codes.add(f"{EVIDENCE_LINEAGE_ABSENT}:{source_id}")
                    accumulator.anomalies.append(
                        AnomalyEntry(
                            lane_id=lane_id,
                            source_id=source_id,
                            code=f"{EVIDENCE_LINEAGE_ABSENT}:{source_id}",
                            detail=record.native_key,
                        )
                    )
                    continue
                try:
                    row = _lifecycle_row(
                        lane_id=lane_id,
                        record=record,
                        quote_currency=quote_currency,
                        synthetic=synthetic,
                        inherited_anomalies=inherited,
                    )
                except JournalReadRejected as exc:
                    anomaly_codes.add(exc.code)
                    accumulator.anomalies.append(
                        AnomalyEntry(
                            lane_id=lane_id,
                            source_id=source_id,
                            code=exc.code,
                            detail=exc.detail,
                        )
                    )
                    continue
                accumulator.lifecycle_rows.append(row)

        observed_classes = sorted(
            {
                ref.evidence_class.value
                for row in accumulator.lifecycle_rows
                for ref in row.evidence_refs
            }
        )
        bound_classes = sorted(
            {
                _BINDING_BY_SOURCE_ID[source_id].evidence_class.value
                for source_id in source_ids
            }
        )
        count = len(accumulator.lifecycle_rows)
        # "no evidence source is bound" and "the bound sources observed
        # nothing" are different states. Only the first one is a
        # no_evidence_reason; the second is carried by the observation count,
        # unlinked_evidence_count and source_anomaly_codes.
        reason = "" if source_ids else LANE_STRUCTURAL_NO_EVIDENCE_REASON[lane_id]
        if count == 0:
            tier = EvidenceTier.UNVERIFIED
        else:
            tier = EvidenceTier.INFERENCE if anomaly_codes else EvidenceTier.FACT

        coverage_rows.append(
            LaneCoverageRow(
                lane_id=lane_id,
                lane_status=entry.lane_status.value,
                activation_status=entry.activation_status.value,
                role=entry.role.value if entry.role is not None else None,
                role_pending_reason=entry.role_pending_reason,
                scheduler_owner=(
                    entry.scheduler_owner.value
                    if entry.scheduler_owner is not None
                    else None
                ),
                writer=entry.writer,
                auto_order_enabled=entry.auto_order_enabled,
                quote_currency=quote_currency,
                synthetic=synthetic,
                source_ids=source_ids,
                evidence_classes=tuple(EvidenceClass(value) for value in bound_classes),
                observed_evidence_classes=tuple(
                    EvidenceClass(value) for value in observed_classes
                ),
                lifecycle_observation_count=count,
                unlinked_evidence_count=len(accumulator.unlinked),
                source_anomaly_codes=tuple(sorted(anomaly_codes)),
                no_evidence_reason=reason,
                evidence_tier=tier,
                as_of=as_of,
            )
        )

    lifecycle_rows = tuple(
        row
        for lane_id in CANONICAL_LANE_IDS
        for row in accumulators[lane_id].lifecycle_rows
    )
    anomalies = tuple(
        entry
        for lane_id in CANONICAL_LANE_IDS
        for entry in accumulators[lane_id].anomalies
    )
    unlinked = tuple(
        entry
        for lane_id in CANONICAL_LANE_IDS
        for entry in accumulators[lane_id].unlinked
    )
    holds = tuple(
        HoldEntry(
            lane_id=row.lane_id,
            observation_id=row.observation_id,
            hold_reason_codes=row.hold_reason_codes,
        )
        for row in lifecycle_rows
        if row.on_hold
    )
    return MockAutoReadModelResponse(
        as_of=as_of,
        manifest=ManifestRef(
            path=J7_SOURCE_BINDING_MANIFEST,
            sha256=J7_SOURCE_BINDING_MANIFEST_SHA256,
        ),
        notes=ReadModelNotes(
            role_semantics=ROLE_SEMANTICS_NOTE,
            scheduler_owner_absent_meaning=SCHEDULER_OWNER_ABSENT_NOTE,
            lineage_requirement=LINEAGE_REQUIREMENT_NOTE,
            aggregation_boundary=AGGREGATION_BOUNDARY_NOTE,
        ),
        source_bindings=EVIDENCE_SOURCE_BINDINGS,
        predecessors=J7_PREDECESSORS,
        ancestor_unknowns=ANCESTOR_UNKNOWNS,
        coverage_rows=tuple(coverage_rows),
        lifecycle_rows=lifecycle_rows,
        anomalies=anomalies,
        anomaly_counts=_counts(entry.code for entry in anomalies),
        holds=holds,
        hold_counts=_counts(
            code for entry in holds for code in entry.hold_reason_codes
        ),
        unlinked_evidence=unlinked,
        unlinked_evidence_counts=_counts(entry.lane_id for entry in unlinked),
    )


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def select_by_decision_intent_id(
    response: MockAutoReadModelResponse, decision_intent_id: str
) -> tuple[LifecycleObservationRow, ...]:
    """Cross-lane fan-out: one intent, every lane, one identical row shape."""

    return tuple(
        row
        for row in response.lifecycle_rows
        if row.decision_intent_id == decision_intent_id
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "AGGREGATION_BOUNDARY_NOTE",
    "ANCESTOR_UNKNOWNS",
    "ANCESTOR_UNKNOWN_ANOMALY_PREFIX",
    "EVIDENCE_LINEAGE_ABSENT",
    "EVIDENCE_SOURCE_BINDINGS",
    "J3A_REVIEW_B_COMPANION",
    "J6C_OWNED_LANE_IDS",
    "J7_PREDECESSORS",
    "J7_SOURCE_BINDING_MANIFEST",
    "J7_SOURCE_BINDING_MANIFEST_SHA256",
    "JOURNAL_LOCATOR_ABSENT",
    "JOURNAL_PATH_ESCAPES_ROOT",
    "JOURNAL_PATH_IS_SYMLINK",
    "JOURNAL_PATH_NOT_REGULAR_FILE",
    "JOURNAL_ROOT_NOT_A_DIRECTORY",
    "JOURNAL_ROOT_UNSET",
    "JOURNAL_ROW_CORRUPT",
    "KIWOOM_MANIFEST_JOURNAL_SEGMENT",
    "KIWOOM_REPO_WRITER_JOURNAL_SEGMENT",
    "LANE_SOURCE_DISCRIMINATORS",
    "LANE_SOURCE_IDS",
    "LANE_STATIC_ANOMALY_CODES",
    "LANE_STRUCTURAL_NO_EVIDENCE_REASON",
    "LINEAGE_REQUIREMENT_NOTE",
    "MANIFEST_LOCATOR_SEGMENT_DIFFERS",
    "READER_SYMBOL_ALLOWLIST",
    "READER_SYMBOL_NOT_ALLOWLISTED",
    "READER_SYMBOL_UNRESOLVED",
    "ROLE_SEMANTICS_NOTE",
    "SCHEDULER_OWNER_ABSENT_NOTE",
    "SCHEMA_VERSION",
    "SOURCE_READ_FAILED",
    "STAGE_EVIDENCE_INSUFFICIENT",
    "SYNTHETIC_LANE_IDS",
    "UNRESOLVED_READER_SYMBOL",
    "DbLedgerSourcePort",
    "EvidenceSourcePort",
    "JournalReadRejected",
    "JournalSourcePort",
    "RawEvidenceRecord",
    "SourceReadResult",
    "build_default_ports",
    "build_read_model",
    "default_journal_root",
    "normalize_stage",
    "read_jsonl_fail_closed",
    "resolve_journal_path",
    "resolve_reader_callable",
    "resolve_reader_symbol",
    "select_by_decision_intent_id",
    "utc_now",
]
