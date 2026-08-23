"""§134차 — the r8 execution authority, and the eight things it still refuses.

Operator decision §134차 (2026-08-21) reads, verbatim:

    「§132차가 바인딩한 r8 봉인 — pre_snapshot_hash
      sha256:4816a1d93da8a6e754d46b98d01e70aab9a82c7720bd86e0d5720583cc66f830 ·
      manifest_sha256
      fa7092a32e09689927228c400dbc97312af4eb1cd9f76d34a208d1fe7f1e8431 ·
      수량/가격 3건 §132차 원문 — 한정으로 dispatch를 허용한다.
      그 외 봉인은 계속 거부한다.」

The first clause is one test: the committed artifact clears every gate. The
second clause — *every other seal stays refused* — is the rest of this file,
and it is the part worth testing, because a cutover that quietly widened the
permission would pass the first test just as well.

Each case takes the **committed artifact's own bytes**, makes one change, and
proves the refusal. Where the change would be caught by the digest alone, the
mutant is *also* registered as ``dispatch_authorized`` so the content check is
exercised rather than shadowed: the question is not "does the digest gate
work" but "if someone got past it, what else refuses".

    M1  a different seal (the superseded r7 digest)          -> refused
    M2  an unregistered digest                                -> refused
    M3  the artifact with one byte changed                    -> refused
    M4  ``operator_authorization`` null                       -> refused
    M5  an expired authority                                  -> refused
    M6  any one row with ``mutation_authorized=false``        -> refused
    M7  a quantity or price that is not the §132차 value       -> refused
    M8  a fourth symbol, a BUY, a MARKET                      -> refused

Every comparison is closed equality against a frozen constant. Nothing here
matches on a name or a substring.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.brokers.binance.spot_demo import d2_remediation_single as d2
from app.services.brokers.binance.spot_demo.d2_remediation_single import (
    D2_ALLOWED_OPERATION_IDS,
    D2_BOUND_ORDERS,
    D2_DISPATCH_AUTHORIZED_DIGESTS,
    D2_PRE_SNAPSHOT_HASH,
    D2_R8_ARTIFACT_MANIFEST_SHA256,
    D2_R8_BINDING_PAYLOAD_SHA256,
    D2_R8_EXECUTION_AUTHORITY_PATH,
    D2_R8_EXECUTION_AUTHORITY_SHA256,
    D2_R8_SEAL_TIMESTAMP_UTC,
    D2_REMEDIATION_ENABLED_ENV,
    D2_SNAPSHOT_SEAL_SHA256,
    D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH,
    D2DispatchNotAuthorized,
    D2ReasonCode,
    D2SealBindingMismatch,
    SealedPayloadRecord,
    load_sealed_authority,
)

# The fakes are the ones the round-1/round-2 suite already uses; reusing them
# means a mutant is proved against the same transport-counting client rather
# than a second, more forgiving one.
from tests.services.brokers.binance.spot_demo.test_d2_remediation_single import (
    FakeExecutionClient,
    FakeLedger,
    make_lease,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# §132차, transcribed. Not recomputed, not re-rounded.
# --------------------------------------------------------------------------

SECTION_132_ORDERS: tuple[tuple[str, str, str], ...] = (
    ("BTCUSDT", "0.00015000", "75421.27000000"),
    ("ETHUSDT", "0.00520000", "2368.46000000"),
    ("USDCUSDT", "5000.00000000", "1.00030000"),
)
SECTION_132_PRE_SNAPSHOT_HASH = (
    "sha256:4816a1d93da8a6e754d46b98d01e70aab9a82c7720bd86e0d5720583cc66f830"
)
SECTION_132_MANIFEST_SHA256 = (
    "fa7092a32e09689927228c400dbc97312af4eb1cd9f76d34a208d1fe7f1e8431"
)

#: Well before the artifact's expiry, so the "it clears" cases assert an
#: authority state rather than a wall-clock accident.
D2_R8_EXPIRY_ISO = "2026-08-22T14:59:59+00:00"
BEFORE_EXPIRY = dt.datetime(2026, 8, 21, 13, 0, tzinfo=dt.UTC)
AFTER_EXPIRY = dt.datetime(2026, 8, 23, 0, 0, tzinfo=dt.UTC)

ARTIFACT = Path(D2_R8_EXECUTION_AUTHORITY_PATH)


def artifact_bytes() -> bytes:
    return ARTIFACT.read_bytes()


def artifact_payload() -> dict[str, Any]:
    return json.loads(artifact_bytes().decode("utf-8"))


def write_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    register: bool = True,
    dispatch_authorized: bool = True,
    pre_snapshot_hash: str = D2_PRE_SNAPSHOT_HASH,
) -> Path:
    """Write a mutant and optionally register its exact-byte digest.

    Registration is monkeypatched module state. Nothing in ``app/`` or
    ``scripts/`` can add an entry at runtime; the shipped map is a
    ``Final[Mapping]`` with three entries, one of them authorized.
    """

    path = tmp_path / "mutant-authority.json"
    raw = json.dumps(payload).encode("utf-8")
    path.write_bytes(raw)
    registry = dict(d2.D2_KNOWN_SEALED_PAYLOADS)
    if register:
        registry[hashlib.sha256(raw).hexdigest()] = SealedPayloadRecord(
            sha256=hashlib.sha256(raw).hexdigest(),
            pre_snapshot_hash=pre_snapshot_hash,
            dispatch_authorized=dispatch_authorized,
            note="mutant fixture",
        )
    monkeypatch.setattr(d2, "D2_KNOWN_SEALED_PAYLOADS", registry)
    return path


def blockers(path: Path, *, now: dt.datetime = BEFORE_EXPIRY) -> tuple[str, ...]:
    return load_sealed_authority(path).dispatch_block_reasons(now=now)


# --------------------------------------------------------------------------
# The permission itself
# --------------------------------------------------------------------------


def test_the_registered_digest_is_the_committed_artifact_bytes() -> None:
    """The echo the operator's ledger records.

    If this fails, the file in the diff is not the file the writer authorizes,
    and no other test in this module means anything.
    """

    assert ARTIFACT.is_file(), D2_R8_EXECUTION_AUTHORITY_PATH
    assert hashlib.sha256(artifact_bytes()).hexdigest() == (
        "ba6518e7cafa16160059aafb22cd304d13793e0f8ea7f035d8fbd8ddc967b00b"
    )
    assert D2_R8_EXECUTION_AUTHORITY_SHA256 == (
        "ba6518e7cafa16160059aafb22cd304d13793e0f8ea7f035d8fbd8ddc967b00b"
    )
    assert D2_DISPATCH_AUTHORIZED_DIGESTS == {D2_R8_EXECUTION_AUTHORITY_SHA256}


def test_the_committed_authority_clears_every_dispatch_gate() -> None:
    """The gate discriminates; it is not a blanket permission either.

    Without this, the eight refusals below would be satisfied by an artifact
    that never authorizes anything, which would prove nothing.
    """

    authority = load_sealed_authority(ARTIFACT)
    assert authority.payload_sha256 == D2_R8_EXECUTION_AUTHORITY_SHA256
    assert authority.record.dispatch_authorized is True
    assert authority.dispatch_block_reasons(now=BEFORE_EXPIRY) == ()
    assert authority.mutation_authorized_symbols == {
        "BTCUSDT",
        "ETHUSDT",
        "USDCUSDT",
    }


def test_the_frozen_constants_are_the_132_values_verbatim() -> None:
    """Requirement ③, checked against the decision text rather than itself."""

    assert D2_PRE_SNAPSHOT_HASH == SECTION_132_PRE_SNAPSHOT_HASH
    assert D2_R8_ARTIFACT_MANIFEST_SHA256 == SECTION_132_MANIFEST_SHA256
    assert (
        tuple(
            (o.symbol, format(o.quantity, "f"), format(o.price, "f"))
            for o in D2_BOUND_ORDERS
        )
        == SECTION_132_ORDERS
    )
    # And §125차's superseded prices are gone from the bound set entirely.
    superseded = {"69266.01000000", "2248.56000000", "1.00072000"}
    assert not superseded & {format(o.price, "f") for o in D2_BOUND_ORDERS}


def test_the_authority_binds_the_r8_seal_without_restating_it() -> None:
    """Requirement ①: the r8 snapshot payload keeps its own bytes.

    The artifact names the snapshot by digest and declares it un-rewritten;
    the snapshot payload stays registered as a *separate*, unauthorized entry.
    """

    binding = artifact_payload()["sealed_snapshot_binding"]
    assert binding["snapshot_payload_rewritten"] is False
    assert binding["binding_payload_sha256"] == D2_R8_BINDING_PAYLOAD_SHA256
    assert binding["artifact_manifest_sha256"] == D2_R8_ARTIFACT_MANIFEST_SHA256
    assert binding["snapshot_seal_sha256"] == D2_SNAPSHOT_SEAL_SHA256
    assert binding["seal_timestamp_utc"] == D2_R8_SEAL_TIMESTAMP_UTC

    assert D2_R8_BINDING_PAYLOAD_SHA256 != D2_R8_EXECUTION_AUTHORITY_SHA256
    snapshot_record = d2.D2_KNOWN_SEALED_PAYLOADS[D2_R8_BINDING_PAYLOAD_SHA256]
    assert snapshot_record.dispatch_authorized is False
    assert snapshot_record.pre_snapshot_hash == D2_PRE_SNAPSHOT_HASH


def test_the_operator_authorization_is_a_decision_reference_not_a_signature() -> None:
    """Requirement ⑤'s honesty half.

    ``operator_authorization`` being non-null is a gate the writer enforces.
    What it contains is a pointer to a recorded decision, and the artifact says
    so rather than presenting itself as cryptographically signed.
    """

    auth = artifact_payload()["operator_authorization"]
    assert auth is not None
    assert auth["kind"] == "OPERATOR_DECISION_LEDGER_REFERENCE"
    assert auth["section"] == "§134차"
    assert "그 외 봉인은 계속 거부한다" in auth["verbatim_ko"]
    assert SECTION_132_PRE_SNAPSHOT_HASH.split(":", 1)[1] in auth["verbatim_ko"]
    assert SECTION_132_MANIFEST_SHA256 in auth["verbatim_ko"]
    assert "not a cryptographic signature" in auth["signature_kind_note"]


# --------------------------------------------------------------------------
# M1 — a different seal
# --------------------------------------------------------------------------


def test_m1_marking_another_registered_seal_authorized_fails_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tripwire §134차 replaced, in its narrowed form.

    The old tripwire refused *any* authorized entry. This one refuses any set
    of authorized entries that is not equal to the one-member permission — so
    it still catches exactly this, which is the failure mode the replacement
    was most likely to introduce.
    """

    r7_digest = "e1c2d250d73ae3bdb631289a7293c35c217b9e5c6e2694d3f8ea572d1835a3aa"
    widened = dict(d2.D2_KNOWN_SEALED_PAYLOADS)
    widened[r7_digest] = SealedPayloadRecord(
        sha256=r7_digest,
        pre_snapshot_hash=D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH,
        dispatch_authorized=True,
        note="r7, wrongly authorized",
    )
    monkeypatch.setattr(d2, "D2_KNOWN_SEALED_PAYLOADS", widened)
    with pytest.raises(d2.D2UnauthorizedOperation) as exc:
        d2._assert_closed_order_set()
    assert exc.value.reason_code is D2ReasonCode.UNAUTHORIZED_OPERATION
    assert r7_digest in str(exc.value)


def test_m1_dropping_the_r8_authority_also_fails_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed equality cuts both ways.

    A registry that authorizes *nothing* is no longer silently fine either: it
    disagrees with the recorded permission, and disagreement is the signal.
    """

    narrowed = dict(d2.D2_KNOWN_SEALED_PAYLOADS)
    narrowed[D2_R8_EXECUTION_AUTHORITY_SHA256] = SealedPayloadRecord(
        sha256=D2_R8_EXECUTION_AUTHORITY_SHA256,
        pre_snapshot_hash=D2_PRE_SNAPSHOT_HASH,
        dispatch_authorized=False,
        note="authority de-registered",
    )
    monkeypatch.setattr(d2, "D2_KNOWN_SEALED_PAYLOADS", narrowed)
    with pytest.raises(d2.D2UnauthorizedOperation):
        d2._assert_closed_order_set()


def test_m1_an_authority_naming_the_superseded_r7_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-level M1: registered *and* authorized, and still refused.

    This is the case the digest gate would normally shadow. It is exercised on
    purpose, because the digest gate is exactly what an operator error in the
    registry removes.
    """

    payload = artifact_payload()
    payload["pre_snapshot_hash"] = D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH
    path = write_authority(
        tmp_path,
        monkeypatch,
        payload,
        pre_snapshot_hash=D2_SUPERSEDED_R7_PRE_SNAPSHOT_HASH,
    )
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_HASH_MISMATCH


@pytest.mark.parametrize(
    "field",
    [
        "binding_payload_sha256",
        "artifact_manifest_sha256",
        "snapshot_seal_sha256",
        "seal_timestamp_utc",
    ],
)
def test_m1_an_authority_naming_a_different_seal_component_is_refused(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    payload["sealed_snapshot_binding"][field] = "0" * 64
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_IDENTITY_MISMATCH
    assert field in str(exc.value)


def test_m1_an_authority_admitting_it_rewrote_the_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement ① as a runtime check, not only as a promise in a docstring."""

    payload = artifact_payload()
    payload["sealed_snapshot_binding"]["snapshot_payload_rewritten"] = True
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "field", ["schema_version", "artifact_kind", "authority_scope"]
)
def test_m1_an_authorized_payload_that_is_not_an_execution_authority_is_refused(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    payload[field] = "something-else"
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_IDENTITY_MISMATCH


def test_m1_the_r8_snapshot_payload_shape_cannot_be_promoted_to_an_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripping the execution-authority block leaves a payload that binds and
    refuses — a snapshot re-registered as authorized does not become one."""

    payload = artifact_payload()
    del payload["sealed_snapshot_binding"]
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert "names no seal" in str(exc.value)


# --------------------------------------------------------------------------
# M2 / M3 — the digest gate
# --------------------------------------------------------------------------


def test_m2_an_unregistered_digest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_authority(tmp_path, monkeypatch, artifact_payload(), register=False)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_UNKNOWN_DIGEST


def test_m2_arbitrary_bytes_are_refused_before_the_json_is_parsed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-an-authority.json"
    path.write_bytes(b"{ this is not json")
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    # Unknown digest, not a JSON error: the bytes never reach the parser.
    assert exc.value.reason_code is D2ReasonCode.SEAL_UNKNOWN_DIGEST


def test_m3_one_changed_byte_in_the_committed_artifact_is_refused(
    tmp_path: Path,
) -> None:
    """No registry monkeypatching: the shipped map is what refuses this."""

    raw = bytearray(artifact_bytes())
    index = raw.index(b"75421.27000000") + 4
    assert raw[index : index + 1] == b"1"
    raw[index : index + 1] = b"2"
    path = tmp_path / "one-byte-off.json"
    path.write_bytes(bytes(raw))
    assert hashlib.sha256(bytes(raw)).hexdigest() != D2_R8_EXECUTION_AUTHORITY_SHA256
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_UNKNOWN_DIGEST


def test_m3_even_a_whitespace_only_reserialisation_is_refused(
    tmp_path: Path,
) -> None:
    """Same JSON, different bytes. The digest is over bytes, not over meaning."""

    path = tmp_path / "reserialised.json"
    path.write_bytes(json.dumps(artifact_payload(), indent=4).encode("utf-8"))
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_UNKNOWN_DIGEST


# --------------------------------------------------------------------------
# M4 / M5 / M6 — the authorization fields, verified from the new artifact
# --------------------------------------------------------------------------


def test_m4_a_null_operator_authorization_blocks_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    payload["operator_authorization"] = None
    path = write_authority(tmp_path, monkeypatch, payload)
    reasons = blockers(path)
    assert any("operator_authorization is null" in r for r in reasons)


@pytest.mark.parametrize(
    ("expiry", "now", "fragment"),
    [
        (None, BEFORE_EXPIRY, "expiry is absent"),
        ("2026-08-22T14:59:59Z", AFTER_EXPIRY, "authority expired at"),
        ("2020-01-01T00:00:00Z", BEFORE_EXPIRY, "authority expired at"),
    ],
)
def test_m5_an_absent_or_expired_authority_blocks_dispatch(
    expiry: str | None,
    now: dt.datetime,
    fragment: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = artifact_payload()
    payload["expiry"] = expiry
    path = write_authority(tmp_path, monkeypatch, payload)
    reasons = blockers(path, now=now)
    assert any(fragment in r for r in reasons), reasons


def test_m5_the_committed_authority_does_expire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped artifact is a one-shot, not a standing permission."""

    authority = load_sealed_authority(ARTIFACT)
    assert authority.expiry == dt.datetime(2026, 8, 22, 14, 59, 59, tzinfo=dt.UTC)
    assert authority.dispatch_block_reasons(now=AFTER_EXPIRY) != ()


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "USDCUSDT"])
def test_m6_any_single_row_losing_mutation_authorized_blocks_dispatch(
    symbol: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    payload["authorized_symbols"]["spot"][symbol]["mutation_authorized"] = False
    path = write_authority(tmp_path, monkeypatch, payload)
    reasons = blockers(path)
    assert any(
        f"mutation_authorized is not true for ['{symbol}']" in r for r in reasons
    )


def test_m6_the_dust_and_quote_rows_stay_unauthorized_in_the_artifact() -> None:
    """§108차's dust attestations and USDT retention are not orders and are not
    authorized to become any."""

    spot = artifact_payload()["authorized_symbols"]["spot"]
    for symbol in ("SOLUSDT", "XRPUSDT", "DOGEUSDT", "USDT"):
        assert spot[symbol]["mutation_authorized"] is False
    authority = load_sealed_authority(ARTIFACT)
    assert authority.mutation_authorized_symbols.isdisjoint(
        {"SOLUSDT", "XRPUSDT", "DOGEUSDT", "USDT"}
    )


# --------------------------------------------------------------------------
# M7 — the quantities and prices
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "field", "value"),
    [
        ("BTCUSDT", "proposed_limit_price_floor", "75421.28000000"),
        ("BTCUSDT", "proposed_limit_price_floor", "69266.01000000"),
        ("BTCUSDT", "proposed_quantity_floor", "0.00016000"),
        ("ETHUSDT", "proposed_limit_price_floor", "2248.56000000"),
        ("ETHUSDT", "proposed_quantity_floor", "0.00530000"),
        ("USDCUSDT", "proposed_limit_price_floor", "1.00072000"),
        ("USDCUSDT", "proposed_quantity_floor", "5001.00000000"),
    ],
)
def test_m7_a_quantity_or_price_that_is_not_the_132_value_is_refused(
    symbol: str,
    field: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Includes the §125차 values explicitly: the superseded numbers must be
    refused like any other foreign value, not accepted as a familiar default."""

    payload = artifact_payload()
    payload["authorized_symbols"]["spot"][symbol]["proposed_one_step"][field] = value
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m7_a_trailing_zero_is_not_a_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decimal comparison is by value, so ``0.00015`` and ``0.00015000`` are the
    same order. The refusals above are about the number, not its spelling."""

    payload = artifact_payload()
    step = payload["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"]
    step["proposed_quantity_floor"] = "0.00015"
    step["proposed_limit_price_floor"] = "75421.2700"
    path = write_authority(tmp_path, monkeypatch, payload)
    assert load_sealed_authority(path).dispatch_block_reasons(now=BEFORE_EXPIRY) == ()


def test_m7_a_drifted_sealed_balance_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed balances are part of the bound identity, not decoration."""

    payload = artifact_payload()
    payload["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"][
        "raw_free_quantity"
    ] = "0.00015958"
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# M8 — the shape of the order set
# --------------------------------------------------------------------------


def test_m8_a_fourth_actionable_symbol_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    spot = payload["authorized_symbols"]["spot"]
    spot["SOLUSDT"] = copy.deepcopy(spot["BTCUSDT"])
    spot["SOLUSDT"].update({"symbol": "SOLUSDT", "asset": "SOL"})
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m8_promoting_a_dust_row_to_actionable_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic shape of a fourth order: reuse a row that is already in
    the artifact and change only its disposition."""

    payload = artifact_payload()
    spot = payload["authorized_symbols"]["spot"]
    spot["SOLUSDT"]["disposition"] = "REVIEWED_SCOPE_LIMIT_CANDIDATE"
    spot["SOLUSDT"]["mutation_authorized"] = True
    spot["SOLUSDT"]["proposed_one_step"] = {
        "side": "SELL",
        "order_type": "LIMIT",
        "time_in_force": None,
        "proposed_quantity_floor": "0.00090000",
        "proposed_limit_price_floor": "100.00000000",
        "raw_free_quantity": "0.00094600",
        "raw_locked_quantity": "0E-8",
    }
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m8_a_symbol_authorized_for_mutation_outside_the_bound_set_blocks_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that used to go unchecked.

    Flipping ``mutation_authorized`` on a non-actionable row cannot create an
    order — the bound set is a constant — but it is a seal claiming more than
    §134차 granted, and that is now a refusal rather than a shrug.
    """

    payload = artifact_payload()
    payload["authorized_symbols"]["spot"]["SOLUSDT"]["mutation_authorized"] = True
    path = write_authority(tmp_path, monkeypatch, payload)
    reasons = blockers(path)
    assert any("outside the bound set: ['SOLUSDT']" in r for r in reasons), reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [("side", "BUY"), ("order_type", "MARKET"), ("time_in_force", "IOC")],
)
def test_m8_a_buy_a_market_or_a_foreign_time_in_force_is_refused(
    field: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    payload["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"][field] = value
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


def test_m8_a_missing_third_order_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = artifact_payload()
    del payload["authorized_symbols"]["spot"]["USDCUSDT"]
    path = write_authority(tmp_path, monkeypatch, payload)
    with pytest.raises(D2SealBindingMismatch) as exc:
        load_sealed_authority(path)
    assert exc.value.reason_code is D2ReasonCode.SEAL_ORDER_SET_MISMATCH


# --------------------------------------------------------------------------
# The dry run, bound to the committed artifact
# --------------------------------------------------------------------------


# ROB-1316: the shipped r8 authority expired 2026-08-22T14:59:59Z by design, and
# the expiry is deliberately evaluated against the real clock (a ``now_fn`` fixed
# in the past must not revive a one-shot). So a test that needs a *live* dispatch
# authority cannot pin the clock — it has to seal its own authority with a future
# expiry. LIVE_EXPIRY is far-future on purpose: a near date would just re-arm the
# same bomb this commit is defusing.
LIVE_EXPIRY = "2099-12-31T23:59:59Z"


def _live_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Seal a copy of the shipped authority whose only change is a live expiry.

    Registration is monkeypatched module state — the shipped ``Final[Mapping]``
    is untouched, so this cannot authorize anything outside the test process.
    """

    payload = artifact_payload()
    payload["expiry"] = LIVE_EXPIRY
    return load_sealed_authority(write_authority(tmp_path, monkeypatch, payload))


def _writer(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeExecutionClient,
    *,
    authority=None,
) -> d2.D2RemediationSingleWriter:
    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")
    lease, grant = make_lease()
    return d2.D2RemediationSingleWriter(
        execution_client=client,  # type: ignore[arg-type]
        authority=load_sealed_authority(ARTIFACT) if authority is None else authority,
        lease=lease,
        lease_grant=grant,
        ledger=FakeLedger(),
    )


@pytest.mark.asyncio
async def test_the_dry_run_is_still_the_default_under_the_r8_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Authorizing dispatch did not make dispatch the default.

    ``execute()`` with no argument mutates nothing *while dispatch is actually
    authorized* — the property most at risk from this cutover. ROB-1316: the
    shipped authority has since expired, and against an expired authority the
    assertion is vacuous (nothing dispatches anyway), so this seals a live
    authority to keep proving the risky case.
    """

    client = FakeExecutionClient()
    writer = _writer(
        monkeypatch, client, authority=_live_authority(tmp_path, monkeypatch)
    )
    assert writer.dispatch_block_reasons() == ()  # precondition: dispatch is armed
    report = await writer.execute()
    assert isinstance(report, d2.D2DryRunReport)
    assert client.submit_calls == []
    assert report.broker_mutation_count == 0
    assert report.dispatch_block_reasons == ()
    # ROB-1316: the armed-evidence assertion lives here now — it can only be made
    # against a live authority, and the shipped one has lapsed.
    assert report.as_evidence()["dispatch_authorized"] is True


@pytest.mark.asyncio
async def test_the_dry_run_request_payload_equals_the_132_values_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the operator reads out of the rehearsal before arming --confirm."""

    client = FakeExecutionClient()
    report = await _writer(monkeypatch, client).execute()
    assert isinstance(report, d2.D2DryRunReport)

    emitted = [op.request_params for op in report.operations]
    assert emitted == [
        {
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": "GTC",
        }
        for symbol, quantity, price in SECTION_132_ORDERS
    ]
    # The order-shape check crossed the transport with the same values, and
    # nothing else did.
    assert [
        (c["symbol"], format(c["qty"], "f"), format(c["price"], "f"))
        for c in client.order_test_calls
    ] == list(SECTION_132_ORDERS)
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_the_dry_run_operations_are_the_three_contract_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeExecutionClient()
    report = await _writer(monkeypatch, client).execute()
    assert isinstance(report, d2.D2DryRunReport)
    assert (
        tuple(op.operation_id for op in report.operations) == D2_ALLOWED_OPERATION_IDS
    )
    evidence = report.as_evidence()
    assert evidence["broker_mutation_count"] == 0
    assert evidence["authority"]["payload_sha256"] == D2_R8_EXECUTION_AUTHORITY_SHA256
    assert evidence["pre_snapshot_hash"] == SECTION_132_PRE_SNAPSHOT_HASH
    # ROB-1316: the shipped r8 authority lapsed at its sealed expiry, so evidence
    # built from it reports dispatch as unauthorized and says why. The armed case
    # is proven in ``test_the_dry_run_is_still_the_default_under_the_r8_authority``
    # against a sealed live authority. Asserting the lapse here keeps this test
    # bound to the real artifact instead of quietly re-arming it.
    assert evidence["dispatch_authorized"] is False
    assert any(
        reason.startswith(f"authority expired at {D2_R8_EXPIRY_ISO}")
        for reason in evidence["dispatch_block_reasons"]
    )


@pytest.mark.asyncio
async def test_confirm_still_refuses_when_the_authority_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer-level counterpart of M4/M5/M6: an incomplete authority raises
    before anything lease-protected runs, and nothing is submitted."""

    payload = artifact_payload()
    payload["operator_authorization"] = None
    path = write_authority(tmp_path, monkeypatch, payload)
    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")
    client = FakeExecutionClient()
    lease, grant = make_lease()
    writer = d2.D2RemediationSingleWriter(
        execution_client=client,  # type: ignore[arg-type]
        authority=load_sealed_authority(path),
        lease=lease,
        lease_grant=grant,
        ledger=FakeLedger(),
    )
    with pytest.raises(D2DispatchNotAuthorized):
        await writer.execute(confirm=True)
    assert client.submit_calls == []


@pytest.mark.asyncio
async def test_a_backdated_clock_cannot_revive_the_expired_r8_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``now_fn`` is an evidence-timestamp seam, not an expiry seam."""

    client = FakeExecutionClient()
    monkeypatch.setenv(D2_REMEDIATION_ENABLED_ENV, "true")
    lease, grant = make_lease()
    writer = d2.D2RemediationSingleWriter(
        execution_client=client,  # type: ignore[arg-type]
        authority=load_sealed_authority(ARTIFACT),
        lease=lease,
        lease_grant=grant,
        ledger=FakeLedger(),
        now_fn=lambda: dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    )
    # The expiry comparison uses the real clock, so this test says the same
    # thing before and after 2026-08-22: the seam is not wired to the gate.
    # ROB-1316: compare the *decision*, not the rendered string — the reason text
    # embeds ``now`` at microsecond precision, so two wall-clock reads a few
    # hundred microseconds apart never compare equal once the authority expires.
    real_now = dt.datetime.now(dt.UTC)
    expected = load_sealed_authority(ARTIFACT).dispatch_block_reasons(now=real_now)
    observed = writer.dispatch_block_reasons()
    assert len(observed) == len(expected)
    prefix = f"authority expired at {D2_R8_EXPIRY_ISO}"
    if expected:
        # Backdating now_fn to 2020 did not clear the real-clock expiry.
        assert any(reason.startswith(prefix) for reason in observed)
    assert [r.split(" (now ")[0] for r in observed] == [
        r.split(" (now ")[0] for r in expected
    ]


# --------------------------------------------------------------------------
# Documentation stays true
# --------------------------------------------------------------------------


def test_the_runbook_quotes_the_authorization_verbatim() -> None:
    runbook = Path("docs/runbooks/binance-spot-demo-d2-remediation.md").read_text(
        encoding="utf-8"
    )
    # The runbook wraps the quote across lines, so this checks the two clauses
    # rather than one exact line: the permission and its explicit limit.
    assert "한정으로 dispatch를 허용한다." in runbook
    assert "그 외 봉인은 계속" in runbook and "거부한다" in runbook
    assert SECTION_132_PRE_SNAPSHOT_HASH in runbook
    assert SECTION_132_MANIFEST_SHA256 in runbook
    assert D2_R8_EXECUTION_AUTHORITY_SHA256 in runbook
    for _symbol, quantity, price in SECTION_132_ORDERS:
        assert quantity in runbook and price in runbook


def test_no_superseded_price_survives_anywhere_in_the_d2_surface() -> None:
    """§125차's prices are gone from code, CLI, artifact, and runbook alike.

    A stale price left in a docstring is the failure this whole round exists to
    avoid: it reads as the bound value to anyone who does not open the seal.
    """

    superseded = ("69266.01", "2248.56", "1.00072")

    # Zero tolerance where a reader could mistake one for a live value.
    for path in (Path("scripts/binance_spot_demo_d2_remediation.py"), ARTIFACT):
        text = path.read_text(encoding="utf-8")
        for price in superseded:
            assert price not in text, f"{path}: superseded price {price}"

    # The writer and the runbook may each name them exactly once, and only in
    # the sentence that says they are superseded — naming them there is what
    # stops a future edit from reintroducing them as if they were current.
    for path in (
        Path("app/services/brokers/binance/spot_demo/d2_remediation_single.py"),
        Path("docs/runbooks/binance-spot-demo-d2-remediation.md"),
    ):
        text = path.read_text(encoding="utf-8")
        for price in superseded:
            assert text.count(price) <= 1, f"{path}: superseded price {price}"
        assert "superseded and must not reappear" in text


def test_the_artifact_is_valid_json_and_declares_its_schema() -> None:
    payload = artifact_payload()
    assert payload["schema_version"] == d2.D2_EXECUTION_AUTHORITY_SCHEMA_VERSION
    assert payload["artifact_kind"] == d2.D2_EXECUTION_AUTHORITY_KIND
    assert payload["authority_scope"] == d2.D2_EXECUTION_AUTHORITY_SCOPE
    assert Decimal(
        payload["authorized_symbols"]["spot"]["BTCUSDT"]["proposed_one_step"][
            "proposed_limit_price_floor"
        ]
    ) == Decimal("75421.27")
