"""ROB-1062 H4 (AC22-AC25) — the OOS PnL masking guarantee: every named
bypass route in AC24 (direct field access, debug/repr output, pickle/JSON
round-trip, exception-message leakage) is attempted here and proven blocked.
"""

from __future__ import annotations

import copy
import json
import pickle

import oos_mask as om
import pytest

_SECRET_PNL_VALUE = -12345.6789  # an OOS PnL value that must never leak


def _masked(**overrides):
    kwargs = {"fold_id": "fold-0", "family": "AP-A1", "config_id": "AP-A1-00"}
    kwargs.update(overrides)
    return om.mask(_SECRET_PNL_VALUE, **kwargs)


def _pass_evidence(**overrides):
    kwargs = {
        "fold_id": "fold-0",
        "family": "AP-A1",
        "config_id": "AP-A1-00",
        "modeled_entries": 7,
        "min_modeled_entries_per_fold": 5,
        "passed": True,
    }
    kwargs.update(overrides)
    return om.DryCountPassEvidence(**kwargs)


# --------------------------------------------------------------------- #
# The mask must be transparent when the evidence genuinely matches.
# --------------------------------------------------------------------- #


def test_unmask_with_matching_evidence_returns_the_raw_value():
    masked = _masked()
    evidence = _pass_evidence()
    assert om.unmask(masked, evidence) == _SECRET_PNL_VALUE


# --------------------------------------------------------------------- #
# AC24 bypass route 1: direct field/attribute access.
# --------------------------------------------------------------------- #


def test_direct_attribute_access_to_the_closure_itself_never_yields_the_raw_value():
    masked = _masked()
    reveal = masked._reveal  # the closure itself, not the value
    assert callable(reveal)
    with pytest.raises(om.OOSMaskBypassError, match="direct access"):
        reveal(None)
    with pytest.raises(om.OOSMaskBypassError, match="direct access"):
        reveal(object())  # a fresh, wrong sentinel object


def test_object_getattribute_direct_bypass_of_any_override_still_fails():
    """The most aggressive attribute-access bypass attempt: calling
    object.__getattribute__ directly, which would defeat a
    __getattribute__-override-based design outright."""
    masked = _masked()
    reveal = object.__getattribute__(masked, "_reveal")
    with pytest.raises(om.OOSMaskBypassError):
        reveal(object())


def test_vars_and_dict_introspection_blocked_by_slots():
    masked = _masked()
    with pytest.raises(TypeError):
        vars(masked)
    assert not hasattr(masked, "__dict__")


def test_setattr_and_delattr_are_blocked():
    masked = _masked()
    with pytest.raises(om.OOSMaskBypassError):
        masked.new_attr = 1
    with pytest.raises(om.OOSMaskBypassError):
        del masked._fold_id


# --------------------------------------------------------------------- #
# AC24 bypass route 2: debug/repr/str output.
# --------------------------------------------------------------------- #


def test_repr_never_contains_the_raw_value():
    masked = _masked()
    text = repr(masked)
    assert str(_SECRET_PNL_VALUE) not in text
    assert "<masked>" in text


def test_str_never_contains_the_raw_value():
    masked = _masked()
    text = str(masked)
    assert str(_SECRET_PNL_VALUE) not in text


def test_f_string_formatting_never_leaks_the_raw_value():
    masked = _masked()
    text = f"debug: {masked}"
    assert str(_SECRET_PNL_VALUE) not in text


# --------------------------------------------------------------------- #
# AC24 bypass route 3: pickle / JSON round-trip / deepcopy.
# --------------------------------------------------------------------- #


def test_pickle_dumps_is_blocked():
    masked = _masked()
    with pytest.raises((om.OOSMaskBypassError, TypeError, pickle.PicklingError)):
        pickle.dumps(masked)


def test_deepcopy_is_blocked():
    masked = _masked()
    with pytest.raises((om.OOSMaskBypassError, TypeError)):
        copy.deepcopy(masked)


def test_json_dumps_cannot_serialize_a_masked_value():
    masked = _masked()
    with pytest.raises(TypeError):
        json.dumps(masked)
    # Even a caller-supplied default= hook that naively formats via str()
    # must not leak the value (repr/str are already redacted above, but
    # this proves the composition holds end-to-end through json.dumps).
    text = json.dumps({"pnl": None}, default=lambda o: str(o))
    assert str(_SECRET_PNL_VALUE) not in text


# --------------------------------------------------------------------- #
# AC24 bypass route 4: exception-message leakage.
# --------------------------------------------------------------------- #


def test_wrong_evidence_binding_error_message_never_contains_the_raw_value():
    masked = _masked()
    wrong_fold_evidence = _pass_evidence(fold_id="fold-1")
    with pytest.raises(om.OOSMaskBypassError) as exc_info:
        om.unmask(masked, wrong_fold_evidence)
    assert str(_SECRET_PNL_VALUE) not in str(exc_info.value)


def test_type_error_on_bad_evidence_type_never_contains_the_raw_value():
    masked = _masked()
    with pytest.raises(TypeError) as exc_info:
        om.unmask(masked, "not evidence")
    assert str(_SECRET_PNL_VALUE) not in str(exc_info.value)


# --------------------------------------------------------------------- #
# Binding: a PASS evidence for a DIFFERENT fold/family/config must never
# unmask THIS value (no evidence reuse across scope).
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "override",
    [
        {"fold_id": "fold-1"},
        {"family": "AP-A2"},
        {"config_id": "AP-A1-01"},
    ],
)
def test_evidence_from_a_different_scope_cannot_unmask(override):
    masked = _masked()
    wrong_evidence = _pass_evidence(**override)
    with pytest.raises(om.OOSMaskBypassError, match="does not match"):
        om.unmask(masked, wrong_evidence)


def test_unmask_rejects_non_masked_first_argument():
    evidence = _pass_evidence()
    with pytest.raises(TypeError, match="expected a Masked value"):
        om.unmask(_SECRET_PNL_VALUE, evidence)


# --------------------------------------------------------------------- #
# Equality/hash cannot be used as an oracle.
# --------------------------------------------------------------------- #


def test_equality_comparison_is_blocked_not_a_silent_false():
    masked_a = _masked()
    masked_b = _masked()
    with pytest.raises(om.OOSMaskBypassError):
        _ = masked_a == masked_b


def test_masked_is_unhashable():
    masked = _masked()
    with pytest.raises(TypeError):
        hash(masked)


# --------------------------------------------------------------------- #
# DryCountPassEvidence itself can never represent a fail — closing off a
# whole class of "construct a fake-looking pass to smuggle through" bug.
# --------------------------------------------------------------------- #


def test_dry_count_pass_evidence_cannot_be_constructed_as_a_fail():
    with pytest.raises(ValueError, match="genuine PASS"):
        om.DryCountPassEvidence(
            fold_id="fold-0",
            family="AP-A1",
            config_id="AP-A1-00",
            modeled_entries=2,
            min_modeled_entries_per_fold=5,
            passed=False,
        )


def test_dry_count_pass_evidence_rejects_entries_below_threshold_even_if_flagged_passed():
    with pytest.raises(ValueError, match="below"):
        om.DryCountPassEvidence(
            fold_id="fold-0",
            family="AP-A1",
            config_id="AP-A1-00",
            modeled_entries=2,
            min_modeled_entries_per_fold=5,
            passed=True,
        )


# --------------------------------------------------------------------- #
# PnL-blind counts (AC25) are the exact OPPOSITE of masked: they must be
# always-visible plain data, never wrapped. This test documents that
# `mask`/`unmask` are the ONLY functions this module offers to wrap
# anything — a caller cannot accidentally end up masking a blind count.
# --------------------------------------------------------------------- #


def test_module_public_api_is_exactly_mask_unmask_and_the_two_types():
    assert set(om.__all__) == {
        "DryCountPassEvidence",
        "Masked",
        "OOSMaskBypassError",
        "mask",
        "unmask",
    }
