"""W5 — the written contract must say what the runtime actually does.

Adversarial review R19. The runtime closed the retry algebra: handler-returned
keys buy nothing, and the only grant is a worker-owned ``PreCoreFailure``
re-checked against durable markers in the database. Three contract surfaces
still described the older, unsafe rule -- and an operator reading them could
conclude that a handler declaring ``mutation_not_started`` earns a replay.

Wrong documentation of an order-adjacent safety rule is a defect in the rule,
so these are pinned as tests rather than left to review.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[4]
_CLAUDE = _REPO / "CLAUDE.md"
_RUNBOOK = _REPO / "docs/runbooks/telegram-callback-durable-inbox.md"
_CONTRACTS = _REPO / "app/services/order_proposals/callback_inbox/contracts.py"

#: Keys a handler might return hoping for a replay. Naming one *next to* a
#: retry verdict is the failure mode: it reads as "return this and it re-runs".
_RETRY_KEYS = ("mutation_not_started", "safe_to_retry", "retryable")


#: How far around a mentioning line to look for its denial. A denial often
#: lands on the next line (or is the sentence that introduces a list), and
#: `IGNORED_HANDLER_RETRY_KEYS`'s own literal sits below its explanation.
_DENIAL_WINDOW = 8


def _retry_verdict_lines(text: str) -> list[str]:
    """Lines that mention a retry key and a retry-granting verdict together."""
    offenders: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not any(key in line for key in _RETRY_KEYS):
            continue
        lowered = line.lower()
        context = "\n".join(
            lines[max(0, index - _DENIAL_WINDOW) : index + _DENIAL_WINDOW + 1]
        ).lower()
        grants = (
            "retry_wait" in lowered
            or "re-runnable" in lowered
            or "rerunnable" in lowered
            or re.search(r"\bretry\b(?!.*ignored)", lowered) is not None
        )
        # An explicit denial is what we want instead. Deliberately narrow:
        # "today's core never sets it" is a *caveat* attached to a grant, not
        # a denial of the grant, and reading it as one is how the runbook's
        # table row survived the first version of this check.
        denies = any(
            marker in context
            for marker in (
                "ignored",
                "buys nothing",
                "buy nothing",
                "not honoured",
                "not honored",
                "no authority",
                "never grants",
                "diagnostic",
                "진단용",
                "무시",
                "권한이 생기지 않",
                "얻지 못",
            )
        )
        if grants and not denies:
            offenders.append(line.strip())
    return offenders


@pytest.mark.parametrize(
    ("label", "path"),
    [("CLAUDE.md", _CLAUDE), ("runbook", _RUNBOOK), ("contracts.py", _CONTRACTS)],
)
def test_no_surface_says_a_handler_returned_flag_can_earn_a_retry(
    label: str, path: pathlib.Path
) -> None:
    """R19 — the three surfaces that still described the unsafe rule."""
    offenders = _retry_verdict_lines(path.read_text(encoding="utf-8"))
    assert not offenders, f"{label} still grants retry to a handler flag: {offenders}"


def test_the_runbook_states_the_worker_owned_rule() -> None:
    """And says the true one, in the retry-algebra section."""
    text = _RUNBOOK.read_text(encoding="utf-8")
    assert "PreCoreFailure" in text, "the runbook never names the only retry grant"
    for phrase in ("handler_entered_at", "pre-core"):
        assert phrase in text, phrase


def test_claude_md_states_the_worker_owned_rule() -> None:
    text = _CLAUDE.read_text(encoding="utf-8")
    assert "PreCoreFailure" in text
    assert "handler_entered_at" in text


def test_the_error_class_comment_matches_the_runtime() -> None:
    """``PRE_CORE_FAILURE``'s own doc comment described the old escape hatch."""
    from app.services.order_proposals.callback_inbox.contracts import (
        IGNORED_HANDLER_RETRY_KEYS,
        RETRYABLE_HANDLER_REASONS,
        ErrorClass,
    )

    # Runtime is unchanged by this commit and must stay closed.
    assert RETRYABLE_HANDLER_REASONS == frozenset()
    assert {"mutation_not_started", "retry", "retryable", "safe_to_retry"} <= (
        IGNORED_HANDLER_RETRY_KEYS
    )

    source = _CONTRACTS.read_text(encoding="utf-8")
    block_start = source.index("class ErrorClass")
    block = source[block_start : source.index("ERROR_CLASSES", block_start)]
    pre_core = block[
        block.index("PRE_CORE_FAILURE") - 400 : block.index("PRE_CORE_FAILURE")
    ]
    for key in _RETRY_KEYS:
        assert key not in pre_core, (
            f"PRE_CORE_FAILURE's comment still offers {key!r} as a way in"
        )
    assert ErrorClass.PRE_CORE_FAILURE.value == "pre_core_failure"


#: The exact wording this commit's sibling removed. Kept verbatim so the
#: detector's strength is itself a pinned property: widening the denial window
#: to stop false positives must never stop catching these.
_REMOVED_UNSAFE_WORDING = (
    (
        "CLAUDE.md",
        "- `retry_wait` \u2190 pre-core \uc2e4\ud328, \ub610\ub294 typed "
        "`mutation_not_started`(\ud604 \ucf54\uc5b4\ub294 \uc808\ub300 "
        "\uc548 \uc500)",
    ),
    (
        "runbook",
        "| typed `mutation_not_started: True` from a handler | `retry_wait` | "
        "`pre_core_failure` | yes (today's core never sets it) |",
    ),
    (
        "contracts.py",
        "    #: The mutating region was provably not entered (pre-core failure, "
        "or the\n    #: typed ``mutation_not_started`` flag). The only "
        "re-runnable class.",
    ),
)


@pytest.mark.parametrize(
    ("label", "wording"),
    _REMOVED_UNSAFE_WORDING,
    ids=[case[0] for case in _REMOVED_UNSAFE_WORDING],
)
def test_the_detector_still_catches_the_wording_it_was_built_for(
    label: str, wording: str
) -> None:
    """Anti-erosion: the checker must still flag the exact removed lines.

    Loosening the detector until the real surfaces pass is the obvious way to
    make this suite green for the wrong reason. Feeding it the original text
    makes that impossible without this test going red.
    """
    assert _retry_verdict_lines(wording), (
        f"the detector would no longer catch the {label} wording it exists for"
    )
