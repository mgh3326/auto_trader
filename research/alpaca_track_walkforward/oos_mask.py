"""ROB-1062 H4 (Run A SS15, AC22-AC25) — the OOS PnL masking guarantee.

This is the single most consequential module in H4: H5's entire PnL-blind
dry-count gate rests on the property that an OOS PnL value produced by this
package CANNOT be read by any caller — API, report, log line, ``__repr__``,
debug helper, pickle/JSON round-trip, or exception message — until an
explicit ``unmask()`` call is presented with a genuine, matching
``DryCountPassEvidence`` (which only H5 is positioned to construct, since
only H5 owns the dry-count gate itself).

Design (why this resists the concrete bypass routes AC24 names):

1. **The raw value is never stored as a readable attribute at all.** It
   lives ONLY inside a closure cell captured by a per-instance gated
   callable (``_reveal``). ``Masked`` has NO slot/attribute whose value IS
   the raw PnL — so even direct, no-holds-barred attribute access
   (``masked._reveal``, or ``object.__getattribute__(masked, "_reveal")``,
   which would trivially defeat a ``__getattribute__``-override-based
   design) only ever recovers a FUNCTION, which itself still demands the
   correct, non-exported sentinel token before it will return anything. A
   slot/attribute-based "private" field (name-mangled or not) would NOT
   have this property: a slot descriptor just returns its stored value to
   ANY attribute-access route, override or no override — that design was
   considered and rejected in favor of the closure (see
   ``tests/test_oos_mask.py::test_direct_attribute_access_to_the_closure_
   itself_never_yields_the_raw_value``, which is the regression test for
   exactly that near-miss).
2. ``__repr__``/``__str__`` never format the raw value — only the public,
   non-sensitive binding (``fold_id``/``family``/``config_id``).
3. ``__reduce__``/``__getstate__`` raise — a ``Masked`` value can never be
   pickled (and therefore never persisted to disk, logged via a pickling
   logger, or survive a ``copy.deepcopy``, which falls back to the same
   protocol).
4. ``__eq__``/``__hash__`` raise — comparison is blocked so a caller cannot
   use repeated equality probes as an oracle to binary-search the hidden
   value.
5. ``unmask()`` additionally verifies the supplied evidence's
   ``(fold_id, family, config_id)`` binding matches the masked value's own
   — a genuine PASS evidence for a DIFFERENT fold/config can never unmask
   THIS value (closing the "reuse someone else's passing evidence" route).

Scope honesty: this is a research-backtest guarantee against the concrete,
named bypass classes in AC24 (direct field access, debug/repr output,
pickle/JSON round-trip, exception-message leakage, evidence reuse across
folds/configs) — it is not a defense against a caller willing to import this
module's own private, non-exported sentinel object and hand it to the
closure directly (``from oos_mask import _AUTHORIZED_UNMASK_TOKEN`` reaches
past every public surface this module exposes). That residual gap is
recorded here explicitly rather than silently, per the same "flag it, don't
hide it" discipline H3 used for its own documented open choices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "DryCountPassEvidence",
    "Masked",
    "OOSMaskBypassError",
    "mask",
    "unmask",
]


class OOSMaskBypassError(RuntimeError):
    """Raised whenever code attempts to read a masked OOS PnL value without
    a matching, genuine dry-count PASS evidence."""


# Not exported (absent from __all__, single leading underscore only as a
# naming convention — see the module docstring's "scope honesty" note for
# what this does and does not defend against).
_AUTHORIZED_UNMASK_TOKEN = object()


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


@dataclass(frozen=True)
class DryCountPassEvidence:
    """H5's dry-count PASS evidence, scoped to exactly ONE
    ``(fold_id, family, config_id)``. This type can only ever represent a
    genuine PASS — ``__post_init__`` refuses to construct a "fail" instance,
    so there is no way to build a plausible-looking-but-fake unmask key by
    accident; H5's own (out-of-H4-scope) dry-count report type is where a
    FAIL outcome is recorded."""

    fold_id: str
    family: str
    config_id: str
    modeled_entries: int
    min_modeled_entries_per_fold: int
    passed: bool

    def __post_init__(self) -> None:
        _int(self.modeled_entries, "modeled_entries")
        _int(self.min_modeled_entries_per_fold, "min_modeled_entries_per_fold")
        if type(self.passed) is not bool:
            raise TypeError("passed must be built-in bool")
        if not self.passed:
            raise ValueError(
                "DryCountPassEvidence can only represent a genuine PASS — "
                "never construct one to represent a fail/insufficient-sample "
                "outcome"
            )
        if self.modeled_entries < self.min_modeled_entries_per_fold:
            raise ValueError(
                f"modeled_entries={self.modeled_entries} is below "
                f"min_modeled_entries_per_fold={self.min_modeled_entries_per_fold} "
                "— this cannot be a genuine PASS"
            )

    def binding_key(self) -> tuple[str, str, str]:
        return (self.fold_id, self.family, self.config_id)


class Masked:
    """An opaque, unreadable wrapper around one OOS PnL value. See module
    docstring for the full bypass-resistance rationale."""

    __slots__ = ("_fold_id", "_family", "_config_id", "_reveal")

    def __init__(
        self, raw_value: Any, *, fold_id: str, family: str, config_id: str
    ) -> None:
        def _reveal(token: object) -> Any:
            if token is not _AUTHORIZED_UNMASK_TOKEN:
                raise OOSMaskBypassError(
                    "direct access to a masked OOS PnL value is forbidden — "
                    "call oos_mask.unmask(masked, evidence) with H5's "
                    "dry-count PASS evidence"
                )
            return raw_value

        object.__setattr__(self, "_fold_id", fold_id)
        object.__setattr__(self, "_family", family)
        object.__setattr__(self, "_config_id", config_id)
        object.__setattr__(self, "_reveal", _reveal)

    # ---- metadata (safe, non-sensitive) -------------------------------- #

    @property
    def fold_id(self) -> str:
        return self._fold_id

    @property
    def family(self) -> str:
        return self._family

    @property
    def config_id(self) -> str:
        return self._config_id

    # ---- bypass-resistance surface ------------------------------------- #

    def __setattr__(self, name: str, value: Any) -> None:
        raise OOSMaskBypassError("Masked values are immutable")

    def __delattr__(self, name: str) -> None:
        raise OOSMaskBypassError("Masked values are immutable")

    def __repr__(self) -> str:
        return (
            f"Masked(fold_id={self._fold_id!r}, family={self._family!r}, "
            f"config_id={self._config_id!r}, value=<masked>)"
        )

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise OOSMaskBypassError(
            "Masked values must never be pickled (would persist raw OOS PnL "
            "to disk unmasked)"
        )

    def __getstate__(self) -> Any:
        raise OOSMaskBypassError(
            "Masked values must never be pickled/deepcopy-serialized"
        )

    def __eq__(self, other: object) -> bool:
        raise OOSMaskBypassError(
            "Masked values cannot be compared — repeated equality probes "
            "could be used as an oracle to infer the raw value"
        )

    __hash__ = None  # type: ignore[assignment]


def mask(raw_value: Any, *, fold_id: str, family: str, config_id: str) -> Masked:
    """Wrap ``raw_value`` (an OOS ``pnl_views.ThreeViewPnL``, a bare float,
    or any other OOS-PnL-shaped value) as masked-by-default, bound to
    exactly one ``(fold_id, family, config_id)``."""
    return Masked(raw_value, fold_id=fold_id, family=family, config_id=config_id)


def unmask(masked: Masked, evidence: DryCountPassEvidence) -> Any:
    """The ONLY sanctioned way to read a masked OOS PnL value: requires a
    genuine ``DryCountPassEvidence`` whose ``(fold_id, family, config_id)``
    binding matches ``masked`` exactly."""
    if type(masked) is not Masked:
        raise TypeError(f"expected a Masked value, got {type(masked)!r}")
    if type(evidence) is not DryCountPassEvidence:
        raise TypeError(f"expected a DryCountPassEvidence, got {type(evidence)!r}")
    if not evidence.passed:
        # Unreachable given DryCountPassEvidence.__post_init__, kept as an
        # explicit, independently-checkable identity (mirrors reason_codes.
        # reconcile_histogram's own belt-and-suspenders re-check).
        raise OOSMaskBypassError("evidence does not represent a PASS")
    masked_binding = (masked.fold_id, masked.family, masked.config_id)
    if masked_binding != evidence.binding_key():
        raise OOSMaskBypassError(
            f"evidence binding {evidence.binding_key()!r} does not match "
            f"this masked value's binding {masked_binding!r} — a PASS "
            "evidence from a different fold/family/config can never unmask "
            "this value"
        )
    return object.__getattribute__(masked, "_reveal")(_AUTHORIZED_UNMASK_TOKEN)
