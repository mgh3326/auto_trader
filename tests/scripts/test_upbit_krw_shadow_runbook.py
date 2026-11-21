"""hk 5852 T9a — the Upbit KRW shadow runbook's commands are the real CLIs' arguments.

Every ``-m scripts.strategy_sleeves`` / ``-m scripts.execution_mandates`` line in
``docs/runbooks/upbit-krw-shadow-sleeves.md`` is parsed with shlex and passed to
that module's real argparse parser (``parse_args``), so choices, required flags
and mutual exclusion are checked, not just flag spelling. The test also pins the
operating rules: every ``--commit`` has an identical preview before it, the four
arms carry the design's values, and nothing the runbook prescribes is live.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from app.services.strategy_sleeves.vocabulary import CANARY_RULES
from scripts import execution_mandates as mandates_cli
from scripts import strategy_sleeves as sleeves_cli

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs/runbooks/upbit-krw-shadow-sleeves.md"
SLEEVE_RUNBOOK = REPO_ROOT / "docs/runbooks/strategy-sleeves.md"
PYTHON = "uv run python"
PARSERS = {
    "scripts.strategy_sleeves": sleeves_cli._parser,
    "scripts.execution_mandates": mandates_cli._parser,
}
ARMS = {
    "upbit-5852-a0": ("at5852.upbit_krw_trend_shadow", "upbit-krw-shadow-a0"),
    "upbit-5852-a1": ("at5852.upbit_krw_trend_shadow", "upbit-krw-shadow-a1"),
    "upbit-5852-b0": ("at5852.upbit_krw_pullback_shadow", "upbit-krw-shadow-b0"),
    "upbit-5852-b1": ("at5852.upbit_krw_pullback_shadow", "upbit-krw-shadow-b1"),
}


def logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        lines.append((current + line).strip())
        current = ""
    if current:
        lines.append(current.strip())
    return lines


def invocations(text: str) -> list[tuple[str, list[str], str]]:
    """(module, argv after the module, raw logical line) for every CLI line.

    argv keeps the global ``--database-url-env NAME`` and the subcommand, so
    ``parse_args`` sees exactly what the operator types.
    """
    found: list[tuple[str, list[str], str]] = []
    for line in logical_lines(text):
        if not line.startswith(PYTHON + " -m scripts."):
            continue
        tokens = shlex.split(line)
        module = tokens[4]  # uv run python -m scripts.<name>
        found.append((module, tokens[5:], line))
    return found


def subcommand(args: list[str]) -> str:
    """The subcommand: argv is ``--database-url-env NAME <subcommand> ...``."""
    return args[2]


def flags(args: list[str]) -> dict[str, str]:
    """Flag -> value for one invocation (the value is "" for a bare flag)."""
    out: dict[str, str] = {}
    body = args[3:]
    i = 0
    while i < len(body):
        token = body[i]
        if not token.startswith("--"):
            i += 1
            continue
        if i + 1 < len(body) and not body[i + 1].startswith("--"):
            out[token] = body[i + 1]
            i += 2
        else:
            out[token] = ""
            i += 1
    return out


@pytest.fixture(scope="module")
def text() -> str:
    return RUNBOOK.read_text()


@pytest.fixture(scope="module")
def parsed(text):
    return invocations(text)


def test_the_runbook_exists_and_drives_both_clis(parsed):
    assert {module for module, _args, _line in parsed} == set(PARSERS)


@pytest.mark.parametrize("module", PARSERS)
def test_every_invocation_parses_with_the_real_argparse(module, parsed):
    parser = PARSERS[module]()
    lines = [args for name, args, _line in parsed if name == module]
    assert lines, module
    for args in lines:
        parser.parse_args(
            args
        )  # SystemExit on any unknown flag, bad choice or missing value


def test_each_commit_has_an_identical_preview_before_it(parsed):
    seen: list[list[str]] = []
    for module, args, _line in parsed:
        if "--commit" not in args:
            seen.append(args)
            continue
        preview = [a for a in args if a != "--commit"]
        assert preview in seen, (module, args)


def test_nothing_the_runbook_prescribes_is_live(parsed):
    for _module, args, line in parsed:
        f = flags(args)
        assert f.get("--state") != "live", line
        assert f.get("--mode") != "live", line
        for forbidden in (
            "--approval-ref",
            "--confirm-account-ref",
            "--confirm-content-sha256",
        ):
            assert forbidden not in f, line
        assert "--restart" not in f and "--loop" not in f, line


def test_the_sleeve_creates_are_four_shadow_go_path_arms_with_the_design_values(parsed):
    creates = [
        flags(args)
        for module, args, _line in parsed
        if module == "scripts.strategy_sleeves" and subcommand(args) == "create"
    ]
    assert {c["--sleeve-key"] for c in creates} == set(ARMS)
    for create in creates:
        key = create["--sleeve-key"]
        assert create["--rule-id"] == ARMS[key][0], key
        assert create["--rule-id"] in CANARY_RULES
        assert create["--account-mode"] == "upbit"
        assert create["--market"] == "crypto"
        assert create["--state"] == "shadow"
        assert create["--execution-path"] == "go"
        assert create["--budget-cap"] == "1950000"
        assert create["--loss-limit"] == "1000000"
        assert create["--min-effect-pct"] == "1.0"
        assert create["--horizon-sessions"] == "30"
        assert create["--max-duration-days"] == "91"
        assert create["--metric"].strip()


def test_the_mandates_are_four_shadow_mandates_at_the_d9_ceilings(parsed):
    proposals = [
        flags(args)
        for module, args, _line in parsed
        if module == "scripts.execution_mandates" and subcommand(args) == "propose"
    ]
    assert {p["--sleeve-key"] for p in proposals} == set(ARMS)
    for proposal in proposals:
        assert proposal["--mode"] == "shadow"
        assert proposal["--per-intent-max-notional-krw"] == "480000"
        assert proposal["--max-open-intents"] == "4"
        assert proposal["--price-band-bps"] == "30"
        assert proposal["--max-intent-ttl-seconds"] == "43200"
        assert proposal["--sleeve-key"] in ARMS


def test_each_mandate_strategy_label_is_the_arm_label(parsed):
    labels = {
        flags(args)["--sleeve-key"]: flags(args)["--strategy"]
        for module, args, _line in parsed
        if module == "scripts.execution_mandates" and subcommand(args) == "propose"
    }
    assert labels == {key: label for key, (_rule, label) in ARMS.items()}


def test_the_runbook_states_the_do_not_list(text):
    for phrase in (
        "`--state live`",
        "`--mode live`",
        "`EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`",
        "스케줄",
    ):
        assert phrase in text, phrase


def test_the_sleeve_runbook_lists_both_new_rule_ids():
    text = SLEEVE_RUNBOOK.read_text()
    for rule_id in (
        "at5852.upbit_krw_trend_shadow",
        "at5852.upbit_krw_pullback_shadow",
    ):
        assert f"`{rule_id}`" in text, rule_id
        assert rule_id in CANARY_RULES
