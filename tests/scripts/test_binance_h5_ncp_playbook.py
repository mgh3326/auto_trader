"""The NCP playbook is checked against the scripts, runbooks and deploy script it names.

``check_playbook`` is one pure function over the playbook text and facts read
from the real scripts. The real playbook must produce no violation; every
declared mutant (a one-edit corruption of the playbook text) must produce the
violation of the invariant it attacks. Invariants are the sentences below,
counted from this docstring; a sentence without a mutant, or a mutant without a
sentence, fails ``test_invariant_sentences_match_the_mutants``.

Invariant sentences (one per key):
- FLAG_SCOPE: each container gets exactly the demo flags its own script needs,
  as ``-e NAME=true`` on its own docker run, and no other ``-e``.
- NO_GLOBAL_ENABLE: no command writes, exports, sources or edits an env file or
  the demo flags; the only commands that mention a flag are docker runs and reads.
- ENV_FILE: the only env file is the shared one the NCP runbooks name, passed as
  the variable.
- NO_SUPERVISOR: no restart policy, detach, scheduler, unit or background launcher.
- REAL_FLAGS: every script argument line parses with that script's own argparse.
- CONFIRM: the runner and the truth gate always carry the per-run confirmation.
- NAMES: the long-lived containers have the fixed names and neither collides with
  a unit the deploy script manages.
- WATCHER_WINDOW: the watcher window and pace in the playbook equal the code.
- OUTPUTS: the example output lines equal what the scripts print.
- CHECKS: the truth-gate table lists the gate's six checks in order.
- XREF: every section reference resolves and the Incident section exists.
- KINDS: the incident table covers exactly the alert kinds the code can send.
- COVERAGE: every command the procedure needs is present.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.brokers.binance.h5 import alerting
from app.services.brokers.binance.h5.alerting import AlertKind, H5Alert, H5Alerter
from app.services.brokers.binance.h5.executor import H5TickResult
from app.services.brokers.binance.h5.state import h5_correlation_id
from app.services.brokers.binance.h5.truth_gate import run_truth_gate
from scripts import binance_h5_heartbeat_watch as watch

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO_ROOT / "docs/runbooks/binance-h5-ncp-manual-playbook.md"
DEMO, FUTURES, ALERT = (
    "BINANCE_H5_DEMO_ENABLED",
    "BINANCE_FUTURES_DEMO_ENABLED",
    "BINANCE_H5_ALERT_ENABLED",
)

# (script, mode) -> (env flags, container name or None, --rm expected)
EXPECTED: dict[tuple[str, str], tuple[frozenset[str], str | None, bool]] = {
    ("binance_h5_demo", "--loop"): (
        frozenset({DEMO, FUTURES, ALERT}),
        "at-h5-demo",
        False,
    ),
    ("binance_h5_truth_gate", ""): (frozenset({DEMO, FUTURES}), None, True),
    ("binance_h5_heartbeat_watch", "--loop"): (
        frozenset({ALERT}),
        "at-h5-watch",
        False,
    ),
    ("binance_h5_heartbeat_watch", "--send-test"): (frozenset({ALERT}), None, True),
    ("binance_h5_demo", "--help"): (frozenset(), None, True),
    ("binance_h5_truth_gate", "--help"): (frozenset(), None, True),
    ("binance_h5_heartbeat_watch", "--help"): (frozenset(), None, True),
}
CONFIRMED = {"binance_h5_demo", "binance_h5_truth_gate"}
FORBIDDEN_WORDS = re.compile(
    r"(--restart|--detach|systemctl|crontab|launchctl|nohup|setsid|disown|"
    r"docker\s+compose|\bcron\b)"
)


# --- facts read from the real code -------------------------------------------


def _capture(func, *args) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        func(*args)
    return out.getvalue().strip()


class _Channel:
    async def send(self, alert: H5Alert) -> bool:
        return True


class _Client:
    async def read_account(self):
        return type("A", (), {"nav_usdt": 1, "per_symbol_isolated_1x": {}})()

    async def get_position_mode(self):
        return type("M", (), {"is_hedge_mode": False})()

    async def get_all_positions(self):
        return []

    async def get_all_open_orders(self):
        return type("O", (), {"orders": []})()


class _State:
    async def list_active_signals(self):
        return ()

    async def list_unresolved_intents(self):
        return ()


class _Ledger:
    async def count_open_lifecycles(self):
        return 0

    async def status_distribution(self):
        return {}


def parse_with_script(script: str, argv: list[str]) -> str | None:
    """None when the script's real argparse accepts ``argv``, else its message."""
    module = sys.modules[f"scripts.{script}"]
    seen: list[argparse.Namespace] = []

    async def fake_run(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    original_run, original_argv = module._run, sys.argv
    module._run, sys.argv = fake_run, [f"{script}.py", *argv]
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                module.main()
            except SystemExit as exit_:
                if exit_.code not in (0, None):
                    return err.getvalue().strip() or f"exit {exit_.code}"
    finally:
        module._run, sys.argv = original_run, original_argv
    return None


def read_facts() -> dict:
    import scripts.binance_h5_demo  # noqa: F401
    import scripts.binance_h5_truth_gate  # noqa: F401

    send_test = _capture(
        lambda: asyncio.run(
            watch._send_test(H5Alerter(channel=_Channel(), enabled=True))
        )
    )
    report = asyncio.run(
        run_truth_gate(client=_Client(), state=_State(), ledger=_Ledger())
    )
    deploy = (REPO_ROOT / "scripts/deploy-ncp-pull.sh").read_text()
    (units,) = re.findall(r"declare -a APP_CONTAINERS=\(([^)]*)\)", deploy)
    handoff = (REPO_ROOT / "docs/runbooks/fill-event-handoff.md").read_text()
    pull = (REPO_ROOT / "docs/runbooks/ncp-pull-deploy.md").read_text()
    return {
        "send_test_line": send_test,
        "tick_line": json.dumps(
            dataclasses.asdict(H5TickResult(1_790_000_000_000, "no_entry")),
            sort_keys=True,
        ),
        "check_names": [c.name for c in report.checks],
        "units": set(units.split()),
        "env_file": re.search(r"--env-file (/root/at-secrets/\.env\.api)", handoff)[1],
        "digest_file": re.search(r"(/root/at-run/deployed-digest)\b", pull)[1],
        "kinds": {k.value for k in AlertKind} - {AlertKind.TEST.value},
        "miss_minutes": alerting.DEFAULT_MISS_MINUTES,
        "poll_seconds": 60,
    }


# --- the checker ---------------------------------------------------------------

KEYS = (
    "FLAG_SCOPE",
    "NO_GLOBAL_ENABLE",
    "ENV_FILE",
    "NO_SUPERVISOR",
    "REAL_FLAGS",
    "CONFIRM",
    "NAMES",
    "WATCHER_WINDOW",
    "OUTPUTS",
    "CHECKS",
    "XREF",
    "KINDS",
    "COVERAGE",
)


def blocks(text: str) -> list[tuple[str, str]]:
    return [(m[1], m[2]) for m in re.finditer(r"```(\w*)\n(.*?)```", text, re.S)]


def logical_lines(body: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", body)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def check_playbook(text: str, facts: dict) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def bad(key: str, message: str) -> None:
        found.append((key, message))

    commands = [
        line
        for lang, body in blocks(text)
        if lang == "bash"
        for line in logical_lines(body)
    ]
    seen_combos: set[tuple[str, str]] = set()

    for line in commands:
        if FORBIDDEN_WORDS.search(line) or re.search(r"(^|\s)-d(\s|$)", line):
            bad("NO_SUPERVISOR", line)
        if re.search(
            r"(^|\s)(export|source)\s|(^|[^|])>>?|\bsed\b|\btee\b", line
        ) and not line.startswith("docker run"):
            bad("NO_GLOBAL_ENABLE", line)
        if "ENABLED" in line and not (
            line.startswith("docker run")
            or line.startswith("grep ")
            or "| grep " in line
        ):
            bad("NO_GLOBAL_ENABLE", line)
        if "$env_file" in line and not (
            line.startswith(("docker run", "grep ", "sha256sum", "env_file="))
        ):
            bad("NO_GLOBAL_ENABLE", line)
        if not line.startswith("docker run"):
            continue
        tokens = shlex.split(line)
        if "$image" not in tokens:
            bad("COVERAGE", line)
            continue
        split = tokens.index("$image")
        options, command = tokens[2:split], tokens[split + 1 :]
        flags: set[str] = set()
        name: str | None = None
        has_rm = network_host = False
        env_files: list[str] = []
        i = 0
        while i < len(options):
            opt = options[i]
            if opt == "--rm":
                has_rm = True
            elif opt == "--network" and options[i + 1 : i + 2] == ["host"]:
                network_host, i = True, i + 1
            elif opt == "--env-file":
                env_files.append(options[i + 1])
                i += 1
            elif opt == "--name":
                name, i = options[i + 1], i + 1
            elif opt == "-e":
                pair = options[i + 1]
                if not re.fullmatch(r"[A-Z0-9_]+=true", pair):
                    bad("FLAG_SCOPE", f"-e {pair}")
                flags.add(pair.split("=")[0])
                i += 1
            else:
                bad("NO_SUPERVISOR", f"unexpected docker option {opt}: {line}")
            i += 1
        if env_files != ["$env_file"]:
            bad("ENV_FILE", f"{env_files}: {line}")
        if not network_host:
            bad("NO_SUPERVISOR", f"missing --network host: {line}")
        if command[:2] != ["/app/.venv/bin/python", "-m"] or not command[2].startswith(
            "scripts."
        ):
            bad("COVERAGE", line)
            continue
        script, argv = command[2].removeprefix("scripts."), command[3:]
        mode = next(
            (m for m in ("--help", "--send-test", "--loop", "--once") if m in argv), ""
        )
        key = (script, mode)
        seen_combos.add(key)
        message = parse_with_script(script, argv)
        if message is not None:
            bad("REAL_FLAGS", f"{line}: {message}")
        if key not in EXPECTED:
            bad("COVERAGE", f"unexpected command {key}: {line}")
            continue
        want_flags, want_name, want_rm = EXPECTED[key]
        if flags != want_flags:
            bad("FLAG_SCOPE", f"{key}: {sorted(flags)} != {sorted(want_flags)}")
        if name != want_name:
            bad("NAMES", f"{key}: {name} != {want_name}")
        if has_rm != want_rm:
            bad("NO_SUPERVISOR", f"{key}: --rm is {has_rm}, expected {want_rm}")
        if name is not None and name in facts["units"]:
            bad("NAMES", f"{name} collides with a deploy-managed unit")
        if script in CONFIRMED and mode != "--help" and "--confirm-demo" not in argv:
            bad("CONFIRM", line)
        if script == "binance_h5_heartbeat_watch" and mode == "--loop":
            for flag, fact in (
                ("--miss-minutes", "miss_minutes"),
                ("--poll-seconds", "poll_seconds"),
            ):
                if flag not in argv or argv[argv.index(flag) + 1] != str(facts[fact]):
                    bad("WATCHER_WINDOW", f"{flag} != {facts[fact]}: {line}")

    for combo in EXPECTED:
        if combo not in seen_combos:
            bad("COVERAGE", f"missing command {combo}")

    if (
        not any(line == f'image="$(cat {facts["digest_file"]})"' for line in commands)
        or f"env_file={facts['env_file']}" not in commands
    ):
        bad("ENV_FILE", "variables block does not use the runbook paths")

    sample = {
        lang_body[1].strip() for lang_body in blocks(text) if lang_body[0] == "text"
    }
    if facts["send_test_line"] not in sample:
        bad("OUTPUTS", f"send-test example missing: {facts['send_test_line']}")
    if facts["tick_line"] not in text.replace("`", ""):
        bad("OUTPUTS", f"tick example missing: {facts['tick_line']}")

    section4 = text.split("## 4.")[1].split("## 5.")[0] if "## 4." in text else ""
    rows = re.findall(r"^\| `([a-z0-9_]+)` \|", section4, re.M)
    if rows != facts["check_names"]:
        bad("CHECKS", f"{rows} != {facts['check_names']}")

    headings = {int(m[1]) for m in re.finditer(r"^## (\d+)\. ", text, re.M)}
    refs = {int(m[1]) for m in re.finditer(r"§(\d+)", text)}
    if not refs <= headings:
        bad("XREF", f"dangling section references {sorted(refs - headings)}")
    if not re.search(r"^## \d+\. Incident response$", text, re.M):
        bad("XREF", "no Incident response section")

    section9 = text.split("Incident response")[-1].split("## 10.")[0]
    kinds = set(re.findall(r"^\| `([a-z_]+)` \|", section9, re.M))
    if kinds != facts["kinds"]:
        bad("KINDS", f"{sorted(kinds)} != {sorted(facts['kinds'])}")
    return found


def violation_keys(text: str, facts: dict) -> set[str]:
    return {key for key, _ in check_playbook(text, facts)}


# --- mutants ---------------------------------------------------------------------


def swap(old: str, new: str):
    def apply(text: str) -> str:
        assert old in text, old
        return text.replace(old, new, 1)

    return apply


def append_command(command: str):
    def apply(text: str) -> str:
        marker = "## 2. Variables"
        assert marker in text
        return text.replace(marker, f"```bash\n{command}\n```\n\n{marker}", 1)

    return apply


RUNNER = '-e BINANCE_H5_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_demo'
WATCHER_LOOP = '--name at-h5-watch --network host --env-file "$env_file" -e BINANCE_H5_ALERT_ENABLED=true'
GATE = '-e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_truth_gate --confirm-demo'

MUTANTS: dict[str, list] = {
    "FLAG_SCOPE": [
        swap(WATCHER_LOOP, WATCHER_LOOP + " -e BINANCE_FUTURES_DEMO_ENABLED=true"),
        swap(
            '-e BINANCE_H5_ALERT_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_demo',
            '"$image" /app/.venv/bin/python -m scripts.binance_h5_demo',
        ),
        swap(WATCHER_LOOP, WATCHER_LOOP + " -e KIS_APP_KEY=true"),
    ],
    "NO_GLOBAL_ENABLE": [
        append_command('echo BINANCE_FUTURES_DEMO_ENABLED=true >> "$env_file"'),
        append_command("export BINANCE_H5_DEMO_ENABLED=true"),
        append_command('sed -i "s/=false/=true/" "$env_file"'),
    ],
    "ENV_FILE": [
        swap(
            '--env-file "$env_file" -e BINANCE_H5_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_truth_gate',
            '--env-file /root/at-secrets/.env.prod -e BINANCE_H5_DEMO_ENABLED=true -e BINANCE_FUTURES_DEMO_ENABLED=true "$image" /app/.venv/bin/python -m scripts.binance_h5_truth_gate',
        ),
        swap("env_file=/root/at-secrets/.env.api", "env_file=/root/at-secrets/.env.h5"),
    ],
    "NO_SUPERVISOR": [
        swap(
            "docker run --name at-h5-demo",
            "docker run --restart unless-stopped --name at-h5-demo",
        ),
        swap("docker run --name at-h5-watch", "docker run -d --name at-h5-watch"),
        append_command("systemctl enable --now at-h5-demo"),
        swap("docker run --name at-h5-demo", "docker run --rm --name at-h5-demo"),
    ],
    "REAL_FLAGS": [
        swap(
            "scripts.binance_h5_demo --loop --confirm-demo",
            "scripts.binance_h5_demo --forever --confirm-demo",
        ),
        swap("--miss-minutes 10", "--miss-minutes 2"),
        swap(
            "scripts.binance_h5_heartbeat_watch --send-test",
            "scripts.binance_h5_heartbeat_watch --send-test --loop",
        ),
    ],
    "CONFIRM": [
        swap(
            "scripts.binance_h5_truth_gate --confirm-demo",
            "scripts.binance_h5_truth_gate",
        ),
        swap(
            "scripts.binance_h5_demo --loop --confirm-demo",
            "scripts.binance_h5_demo --loop",
        ),
    ],
    "NAMES": [
        swap("--name at-h5-demo", "--name at-worker"),
        swap("--name at-h5-watch", "--name at-h5-demo"),
    ],
    "WATCHER_WINDOW": [
        swap("--miss-minutes 10", "--miss-minutes 15"),
        swap("--poll-seconds 60", "--poll-seconds 30"),
    ],
    "OUTPUTS": [
        swap(
            '{"delivered": true, "event": "alert_test"}',
            '{"event": "alert_test", "delivered": true}',
        ),
        swap(
            '{"decision_ts": 1790000000000, "detail": null, "event": "no_entry", "signal_keys": []}',
            '{"event": "no_entry"}',
        ),
    ],
    "CHECKS": [
        swap("| `no_open_orders` |", "| `no_orders` |"),
        swap("| `one_way_position_mode` |", "| `account_isolated_1x` |"),
    ],
    "XREF": [
        swap("## 9. Incident response", "## 9. Handling"),
        swap("(§4)", "(§14)"),
    ],
    "KINDS": [
        swap("| `heartbeat_missed` |", "| `heartbeat` |"),
        swap("| `stopped` |", "| `stopped` | x | y |\n| `crashed` |"),
    ],
    "COVERAGE": [
        swap("docker run --name at-h5-watch", "docker ps --name at-h5-watch"),
        swap(
            "scripts.binance_h5_heartbeat_watch --send-test",
            "scripts.binance_h5_heartbeat_watch --help",
        ),
    ],
}


@pytest.fixture(scope="module")
def facts() -> dict:
    return read_facts()


@pytest.fixture(scope="module")
def playbook() -> str:
    return PLAYBOOK.read_text()


def test_the_real_playbook_has_no_violation(playbook, facts):
    assert check_playbook(playbook, facts) == []


def test_invariant_sentences_match_the_mutants():
    sentences = [
        line[2:].split(":", 1)[0]
        for line in (__doc__ or "").splitlines()
        if line.startswith("- ") and ": " in line
    ]
    assert sentences == list(KEYS) == list(MUTANTS)
    assert all(MUTANTS[key] for key in KEYS)


@pytest.mark.parametrize(
    ("key", "index"),
    [(key, i) for key, group in MUTANTS.items() for i in range(len(group))],
)
def test_every_mutant_is_caught_by_its_own_invariant(playbook, facts, key, index):
    mutated = MUTANTS[key][index](playbook)
    assert mutated != playbook
    assert key in violation_keys(mutated, facts), check_playbook(mutated, facts)


def test_the_script_arguments_in_the_playbook_are_the_real_ones(playbook):
    commands = [
        shlex.split(line)
        for lang, body in blocks(playbook)
        if lang == "bash"
        for line in logical_lines(body)
        if line.startswith("docker run")
    ]
    assert len(commands) == len(EXPECTED)
    assert parse_with_script("binance_h5_demo", ["--loop"]) is None
    assert parse_with_script("binance_h5_demo", []) is not None  # a mode is required
    assert parse_with_script("binance_h5_demo", ["--bogus"]) is not None


# --- facts the playbook states in prose ---------------------------------------------


def test_correlation_formula_matches_the_code(playbook):
    key = "BTCUSDT|2026-09-28 13:00|BUY|100.00"
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    assert h5_correlation_id(key) == "binance-h5:" + digest
    assert "printf '%s' \"$signal_key\" | sha256sum | cut -c1-24" in playbook
    assert "first 24 hex characters" in playbook and "`binance-h5:`" in playbook


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="no GNU sha256sum")
def test_the_shell_pipeline_gives_the_code_value():
    key = "SOLUSDT|2026-10-05 09:00|SELL|150.5"
    out = subprocess.run(
        ["bash", "-c", "printf '%s' \"$signal_key\" | sha256sum | cut -c1-24"],
        env={"signal_key": key, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert len(out) == 24
    assert "binance-h5:" + out == h5_correlation_id(key)


def test_every_bash_block_is_valid_shell(playbook, tmp_path):
    for index, (lang, body) in enumerate(blocks(playbook)):
        if lang != "bash":
            continue
        path = tmp_path / f"block{index}.sh"
        path.write_text(body)
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, (body, result.stderr)


def test_prose_claims_equal_the_code(playbook):
    assert alerting.REPEAT_AFTER.total_seconds() == 6 * 3600
    assert "at most every six hours" in playbook
    assert f"older than {alerting.DEFAULT_MISS_MINUTES} minutes" in playbook
    assert alerting.PLAYBOOK_DOC == "docs/runbooks/binance-h5-ncp-manual-playbook.md"
    assert PLAYBOOK.relative_to(REPO_ROOT).as_posix() == alerting.PLAYBOOK_DOC


def test_h5_and_new_scripts_never_reference_a_live_broker_credential():
    live = re.compile(
        r"\b(kis|upbit|toss|kiwoom|nhplug|alpaca|hermes)_[a-z_]*"
        r"(key|secret|token|account|client|webhook)\b",
        re.I,
    )
    paths = sorted((REPO_ROOT / "app/services/brokers/binance/h5").glob("*.py")) + [
        REPO_ROOT / "scripts/binance_h5_demo.py",
        REPO_ROOT / "scripts/binance_h5_truth_gate.py",
        REPO_ROOT / "scripts/binance_h5_heartbeat_watch.py",
    ]
    assert len(paths) >= 16
    assert [p.name for p in paths if live.search(p.read_text())] == []
