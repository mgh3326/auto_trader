"""ROB-1040 CRS-24 CORR-1 real-corpus input-binding seam.

Covers the ONLY intentional opening of the sealed CRS-24 evidence module:
``CampaignInputBinding``/``open_real_corpus_campaign_context``/
``build_real_corpus_evidence``. Every fixture here is synthetic-in-shape
(small, locally constructed) -- none of it touches the real ROB-941 corpus.

The frozen synthetic path (``build_frozen_synthetic_evidence`` and friends)
is exercised elsewhere (``test_rob1040_crs24_evidence_cli.py``); this file's
job is to prove the new real-corpus posture is (a) reachable only through
its own dedicated entry points, (b) unable to reuse any frozen synthetic pin,
and (c) produces a structurally well-formed, reconciled evidence payload
whose posture/authority differ from the synthetic one -- all without a
single OOS incidence number being asserted against a "real" value (there is
no real corpus here at all).
"""

from __future__ import annotations

import dataclasses

import pytest
import rob1040_crs24_evidence as evidence_module
from rob1040_crs24_evidence import (
    FROZEN_SYNTHETIC_EVIDENCE_SHA256,
    CampaignInputBinding,
    build_real_corpus_evidence,
    open_real_corpus_campaign_context,
)
from rob1040_crs24_feasibility import (
    EntryReference,
    ExitPresence,
    ReferenceSurface,
    RunAuthorityClosedError,
    expected_entry_reference_keys,
    expected_exit_presence_keys,
)
from rob1040_crs24_features import CRSFeatureGenerator
from rob1040_crs24_synthetic import (
    SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256,
    SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256,
    SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256,
    SYNTHETIC_FIXTURE_CONTENT_SHA256,
    SYNTHETIC_FIXTURE_VERSION,
    build_synthetic_fixture,
)

_FAKE_MANIFEST_SHA256 = "1" * 64


def _empty_binding(
    *, version: str = "rob1040.crs24.corr1.real_corpus.v1"
) -> CampaignInputBinding:
    """The smallest possible valid ``refrozen_real_corpus`` binding: an empty
    complete-bar snapshot and an all-absent reference surface.  This is not a
    real corpus and is not shaped like one -- it exists purely to exercise
    the plumbing (context open -> full campaign evaluation -> evidence seal)
    cheaply and without any incidence-bearing content."""
    generator = CRSFeatureGenerator({"XRPUSDT": (), "DOGEUSDT": (), "SOLUSDT": ()})
    entries = tuple(
        EntryReference(key, None) for key in expected_entry_reference_keys()
    )
    exit_presence = tuple(
        ExitPresence(key, False) for key in expected_exit_presence_keys()
    )
    references = ReferenceSurface(entries, exit_presence)
    return CampaignInputBinding.for_real_corpus(
        version=version,
        generator=generator,
        references=references,
        corpus_manifest_content_sha256=_FAKE_MANIFEST_SHA256,
    )


def test_empty_real_corpus_binding_round_trips_through_for_real_corpus() -> None:
    binding = _empty_binding()
    assert binding.posture == "refrozen_real_corpus"
    assert (
        binding.snapshot_sha256_pin
        == CRSFeatureGenerator(
            {"XRPUSDT": (), "DOGEUSDT": (), "SOLUSDT": ()}
        ).snapshot_sha256
    )
    assert binding.extra_authority == (
        ("corpus_manifest_content_sha256", _FAKE_MANIFEST_SHA256),
    )


@pytest.mark.parametrize(
    "field",
    (
        "snapshot_sha256_pin",
        "entry_source_sha256_pin",
        "exit_presence_source_sha256_pin",
        "fixture_content_sha256_pin",
    ),
)
def test_real_corpus_binding_cannot_reuse_any_synthetic_pin(field: str) -> None:
    generator = CRSFeatureGenerator({"XRPUSDT": (), "DOGEUSDT": (), "SOLUSDT": ()})
    entries = tuple(
        EntryReference(key, None) for key in expected_entry_reference_keys()
    )
    exit_presence = tuple(
        ExitPresence(key, False) for key in expected_exit_presence_keys()
    )
    references = ReferenceSurface(entries, exit_presence)
    kwargs = {
        "posture": "refrozen_real_corpus",
        "version": "rob1040.crs24.corr1.real_corpus.v1",
        "generator": generator,
        "references": references,
        "snapshot_sha256_pin": generator.snapshot_sha256,
        "entry_source_sha256_pin": references.entry_source_sha256,
        "exit_presence_source_sha256_pin": references.exit_presence_source_sha256,
        "fixture_content_sha256_pin": evidence_module.fixture_content_sha256(
            generator, references
        ),
        "extra_authority": (),
    }
    synthetic_pin_by_field = {
        "snapshot_sha256_pin": SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256,
        "entry_source_sha256_pin": SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256,
        "exit_presence_source_sha256_pin": SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256,
        "fixture_content_sha256_pin": SYNTHETIC_FIXTURE_CONTENT_SHA256,
    }
    kwargs[field] = synthetic_pin_by_field[field]
    with pytest.raises(ValueError, match="must not reuse"):
        CampaignInputBinding(**kwargs)


def test_synthetic_posture_binding_must_reuse_the_exact_frozen_pins() -> None:
    fixture = build_synthetic_fixture()
    generator = CRSFeatureGenerator(fixture.bars_by_symbol())
    with pytest.raises(ValueError, match="synthetic posture must reuse"):
        CampaignInputBinding(
            posture="frozen_synthetic_fixture",
            version=SYNTHETIC_FIXTURE_VERSION,
            generator=generator,
            references=fixture.references,
            snapshot_sha256_pin=generator.snapshot_sha256,
            entry_source_sha256_pin=fixture.references.entry_source_sha256,
            exit_presence_source_sha256_pin=(
                fixture.references.exit_presence_source_sha256
            ),
            fixture_content_sha256_pin=evidence_module.fixture_content_sha256(
                generator, fixture.references
            ),
            extra_authority=(("stray", "value"),),  # non-empty: forbidden for synthetic
        )


def test_open_real_corpus_context_rejects_a_synthetic_postured_binding() -> None:
    """Even a LEGITIMATELY pinned synthetic-posture binding (built from the
    real frozen fixture, matching every real SYNTHETIC_* constant) is refused
    by the real-corpus entry point -- the posture gate is enforced
    independently of pin correctness, so a synthetic binding can never reach
    ``build_real_corpus_evidence``."""
    fixture = build_synthetic_fixture()
    generator = CRSFeatureGenerator(fixture.bars_by_symbol())
    binding = CampaignInputBinding(
        posture="frozen_synthetic_fixture",
        version=SYNTHETIC_FIXTURE_VERSION,
        generator=generator,
        references=fixture.references,
        snapshot_sha256_pin=SYNTHETIC_COMPLETE_BAR_SNAPSHOT_SHA256,
        entry_source_sha256_pin=SYNTHETIC_ENTRY_REFERENCE_SOURCE_SHA256,
        exit_presence_source_sha256_pin=SYNTHETIC_EXIT_PRESENCE_SOURCE_SHA256,
        fixture_content_sha256_pin=SYNTHETIC_FIXTURE_CONTENT_SHA256,
        extra_authority=(),
    )
    with pytest.raises(RunAuthorityClosedError, match="refrozen_real_corpus"):
        open_real_corpus_campaign_context(binding)
    with pytest.raises(RunAuthorityClosedError, match="refrozen_real_corpus"):
        build_real_corpus_evidence(binding)


def test_open_real_corpus_context_rejects_non_binding_objects() -> None:
    with pytest.raises(RunAuthorityClosedError, match="refrozen_real_corpus"):
        open_real_corpus_campaign_context(object())  # type: ignore[arg-type]


def test_empty_real_corpus_evidence_reconciles_with_zero_planned() -> None:
    binding = _empty_binding()
    evidence = build_real_corpus_evidence(binding)
    assert evidence.totals.scheduled == 24 * 56 == 1_344
    assert evidence.totals.planned == 0
    assert evidence.totals.occupied == 0
    payload = evidence.to_payload()
    assert payload["authorities"]["input"]["posture"] == "refrozen_real_corpus"
    assert payload["authorities"]["input"]["fixture_version"] == binding.version
    assert payload["authorities"]["input"]["real_corpus_authority"] == {
        "corpus_manifest_content_sha256": _FAKE_MANIFEST_SHA256,
    }
    assert evidence.evidence_sha256 != FROZEN_SYNTHETIC_EVIDENCE_SHA256


def test_two_real_corpus_bindings_cannot_mix_contexts() -> None:
    first_binding = _empty_binding()
    second_binding = _empty_binding(version="rob1040.crs24.corr1.real_corpus.v2")
    first_context = open_real_corpus_campaign_context(first_binding)
    second_context = open_real_corpus_campaign_context(second_binding)
    first_cells = first_context.cells()
    second_cells = second_context.cells()
    mixed = (first_cells[0], *second_cells[1:])
    with pytest.raises(RunAuthorityClosedError, match="not issued by this"):
        first_context.seal_cells(mixed)


def test_real_corpus_binding_generator_and_references_are_rechecked_at_use() -> None:
    binding = _empty_binding()
    context = open_real_corpus_campaign_context(binding)
    # The empty fixture has every exit_presence row `present=False`; flipping
    # one to `True` changes `exit_presence_source_sha256` away from the pin
    # captured at `for_real_corpus` construction time, so the next use must
    # be refused even though object identity is unchanged.
    exit_rows = binding.references.exit_presence
    object.__setattr__(
        binding.references,
        "exit_presence",
        (dataclasses.replace(exit_rows[0], present=True), *exit_rows[1:]),
    )
    with pytest.raises(
        RunAuthorityClosedError, match="exit-presence pin changed at use"
    ):
        context.cells()


def test_for_real_corpus_captures_pins_from_the_generator_and_references() -> None:
    """``for_real_corpus`` never accepts a bare caller-supplied pin string --
    every pin is read back from the bound objects, so a caller cannot forge a
    mismatched pin through this constructor."""
    binding = _empty_binding()
    assert binding.snapshot_sha256_pin == binding.generator.snapshot_sha256
    assert binding.entry_source_sha256_pin == binding.references.entry_source_sha256
    assert (
        binding.exit_presence_source_sha256_pin
        == binding.references.exit_presence_source_sha256
    )


def test_for_real_corpus_rejects_a_malformed_manifest_hash() -> None:
    generator = CRSFeatureGenerator({"XRPUSDT": (), "DOGEUSDT": (), "SOLUSDT": ()})
    entries = tuple(
        EntryReference(key, None) for key in expected_entry_reference_keys()
    )
    exit_presence = tuple(
        ExitPresence(key, False) for key in expected_exit_presence_keys()
    )
    references = ReferenceSurface(entries, exit_presence)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        CampaignInputBinding.for_real_corpus(
            version="rob1040.crs24.corr1.real_corpus.v1",
            generator=generator,
            references=references,
            corpus_manifest_content_sha256="not-a-hash",
        )
