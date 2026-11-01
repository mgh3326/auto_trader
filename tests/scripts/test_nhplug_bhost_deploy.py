"""B-HOST uses the DEFAULT blue/green pair and a one-shot host witness only."""

from __future__ import annotations

import functools
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest

import app.services.nhplug_mock.lease_host as lease_host
import scripts.nhplug_t14_host_witness as witness
from app.services.nhplug_mock.lease_host import INIT_PID_NS
from tests.scripts.test_deploy_ncp_pull_rollback import OLD, _run

MOUNT = "/etc/machine-id:/etc/machine-id:ro"
DEFAULT_COLORS = {"at-mcp-blue", "at-mcp-green"}


def _pid_modes(argv: Sequence[str]) -> list[str]:
    """Every docker PidMode in ``argv``, in either spelling.

    Docker accepts ``--pid=host``, ``--pid host`` and quoted forms such as
    ``--pid="host"``; an exact ``"--pid=host"`` token match misses all but the
    first. Surrounding quotes are stripped so a raw (unparsed) token is read the
    same as its shell-parsed value.
    """

    modes: list[str] = []
    for index, token in enumerate(argv):
        if token == "--pid" and index + 1 < len(argv):
            modes.append(argv[index + 1].strip("\"'"))
        elif token.startswith("--pid="):
            modes.append(token.removeprefix("--pid=").strip("\"'"))
    return modes


def _uses_host_pid(argv: Sequence[str]) -> bool:
    return "host" in _pid_modes(argv)


def _assert_no_host_pid(calls: Sequence[Sequence[str]]) -> None:
    assert calls and not any(_uses_host_pid(call) for call in calls)


def _docker_run_options(command: str) -> list[str]:
    """Docker options of the first ``docker run`` in ``command`` (before the image)."""

    joined = command.replace("\\\n", " ")
    words = shlex.split(joined.split("docker run", 1)[1], comments=True)
    options: list[str] = []
    for word in words:
        if word == "$image" or word.startswith("ghcr.io/"):
            break
        options.append(word)
    return options


def _runs(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call and call[0] == "run" and "--name" in call]


def _name(call: list[str]) -> str:
    return call[call.index("--name") + 1]


@pytest.mark.parametrize("args", [(), ("--rollback",)])
@pytest.mark.parametrize("active_color", ["blue", "green"])
def test_only_default_mcp_color_gets_machine_id_on_promotion(
    tmp_path: Path, args: tuple[str, ...], active_color: str
) -> None:
    result, calls, _, _ = _run(tmp_path, args=args, active_mcp_color=active_color)
    assert result.returncode == 0, result.stderr
    runs = _runs(calls)
    mounted = {_name(call) for call in runs if MOUNT in call}
    assert mounted <= DEFAULT_COLORS
    assert len(mounted) == 1
    color = next(iter(mounted))
    call = next(call for call in runs if _name(call) == color)
    assert call.count(MOUNT) == 1
    # Every long-running container keeps its own PID namespace.
    _assert_no_host_pid(runs)


@pytest.mark.parametrize("active_color", ["blue", "green"])
def test_switch_and_failure_rollback_keeps_mount(
    tmp_path: Path, active_color: str
) -> None:
    result, calls, _, _ = _run(
        tmp_path,
        fail_name="at-mcp-kiwoom",
        active_mcp_color=active_color,
        both_mcp_colors_present=True,
    )
    assert result.returncode != 0
    runs = _runs(calls)
    mounted = {_name(call) for call in runs if MOUNT in call}
    target = f"at-mcp-{'green' if active_color == 'blue' else 'blue'}"
    assert mounted == {target}
    assert any(_name(call) == target and OLD in call for call in runs)
    assert sum(_name(call) == target and MOUNT in call for call in runs) == 2
    assert all(MOUNT not in call for call in runs if _name(call) not in DEFAULT_COLORS)
    _assert_no_host_pid(runs)


def test_host_witness_command_is_ephemeral_host_pid_only() -> None:
    wrapper = (
        Path(__file__).resolve().parents[2] / "scripts/nhplug-t14-host-witness.sh"
    ).read_text()
    options = _docker_run_options(wrapper)
    assert _pid_modes(options) == ["host"]
    assert "--rm" in options
    assert options[options.index("--network") + 1] == "none"
    assert "-v /etc/machine-id:/etc/machine-id:ro" in wrapper
    assert "-p " not in wrapper and "--publish" not in wrapper
    assert "python -m scripts.nhplug_t14_host_witness" in wrapper


def test_runbook_does_not_promise_host_visibility() -> None:
    """The runbook runs the installed wrapper and never expects a verified scan."""

    runbook = (
        Path(__file__).resolve().parents[2] / "docs/runbooks/nhplug-mock-smoke.md"
    ).read_text()
    assert "/root/at-run/nhplug-t14-host-witness.sh --self-check" in runbook
    assert "install -m 0750 scripts/nhplug-t14-host-witness.sh" in runbook
    assert "예상 출력은 `host_pid_visibility_unverified`" in runbook
    assert "출력은 `host_pid_visibility_verified`다" not in runbook
    witness_runs = [
        _docker_run_options(span)
        for span in re.findall(r"`([^`]*docker run[^`]*)`", runbook)
    ]
    assert witness_runs
    assert all(_pid_modes(options) == ["host"] for options in witness_runs)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--pid=host", "--name", "x"],
        ["run", "--pid", "host", "--name", "x"],
        ["run", '--pid="host"', "--name", "x"],
        ["run", "--pid='host'", "--name", "x"],
        ["run", "--pid", '"host"', "--name", "x"],
    ],
    ids=["equals", "two-arg", "equals-dquoted", "equals-squoted", "two-arg-dquoted"],
)
def test_host_pid_in_a_forbidden_place_is_red_in_every_spelling(
    argv: list[str],
) -> None:
    """The negative-test guard must fail (assertion) for each spelling."""

    assert _uses_host_pid(argv)
    with pytest.raises(AssertionError):
        _assert_no_host_pid([["run", "--name", "clean"], argv])


@pytest.mark.parametrize(
    "command",
    [
        "docker run --rm --pid host --network none img",
        'docker run --rm --pid="host" --network none img',
        "docker run --rm \\\n  --pid 'host' --network none img",
    ],
)
def test_text_scanner_reads_every_pid_spelling(command: str) -> None:
    assert _pid_modes(_docker_run_options(command)) == ["host"]


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--name", "x"],
        ["run", "--pid=container:abc", "--name", "x"],
        ["run", "--pid", "container:abc"],
        ["run", "--pid-ns", "host"],
        ["run", "--pid"],
    ],
)
def test_non_host_pid_modes_are_not_host(argv: list[str]) -> None:
    assert not _uses_host_pid(argv)
    _assert_no_host_pid([argv])


def test_witness_args_after_the_image_are_not_docker_pid_options() -> None:
    options = _docker_run_options(
        'docker run --rm --pid=host "$image" python -m x --pid 1'
    )
    assert _pid_modes(options) == ["host"]


# ---------------------------------------------------------------------------
# The witness CLI answers from its own scan of a disposable /proc tree.

MACHINE = "0123456789abcdef0123456789abcdef"
BOOT = "01234567-89ab-cdef-0123-456789abcdef"
SIBLING_NS = "4026532100"
LEASE_NS = "4026532200"
LEASE_ARGS = [
    "--machine-id",
    MACHINE,
    "--boot-id",
    BOOT,
    "--pid-ns",
    LEASE_NS,
    "--pid",
    "1",
    "--process-start",
    "777",
]


def _link(path: Path, namespace: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(f"pid:[{namespace}]")


def _witness_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    own: str,
    entries: dict[str, str],
) -> Path:
    machine = tmp_path / "machine-id"
    machine.write_text(MACHINE + "\n")
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT + "\n")
    _link(proc / "self/ns/pid", own)
    _link(proc / "1/ns/pid", own)
    for pid, namespace in entries.items():
        _link(proc / pid / "ns/pid", namespace)
    monkeypatch.setattr(witness, "current_lease_identity", lambda: None)
    monkeypatch.setattr(
        witness,
        "host_pid_namespaces",
        functools.partial(lease_host.host_pid_namespaces, proc_root=proc),
    )
    monkeypatch.setattr(
        witness,
        "process_gone_on_lease_host",
        functools.partial(
            lease_host.process_gone_on_lease_host, machine_id=machine, proc_root=proc
        ),
    )
    return proc


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    rc = witness.main(list(argv))
    return rc, capsys.readouterr().out.strip()


def test_witness_in_sibling_container_is_neither_verified_nor_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """B2: a private namespace naming itself as host verifies and proves nothing."""

    _witness_host(tmp_path, monkeypatch, own=SIBLING_NS, entries={"9": SIBLING_NS})
    assert _cli(capsys, "--host-pid-ns", SIBLING_NS, "--self-check") == (
        1,
        "host_pid_visibility_unverified",
    )
    assert _cli(capsys, "--host-pid-ns", SIBLING_NS, *LEASE_ARGS) == (
        1,
        "host_pid_visibility_unverified",
    )


def test_witness_with_one_unreadable_link_is_neither_verified_nor_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """B1: a ptrace-denied link (seen as a directory) fails both answers closed."""

    proc = _witness_host(tmp_path, monkeypatch, own=INIT_PID_NS, entries={})
    (proc / "77/ns/pid").mkdir(parents=True)
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, "--self-check") == (
        1,
        "host_pid_visibility_unverified",
    )
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, *LEASE_ARGS) == (
        1,
        "host_pid_visibility_unverified",
    )


def test_witness_host_namespace_must_match_outside_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _witness_host(tmp_path, monkeypatch, own=INIT_PID_NS, entries={})
    assert _cli(capsys, "--host-pid-ns", SIBLING_NS, "--self-check")[0] == 1
    assert _cli(capsys, "--host-pid-ns", SIBLING_NS, *LEASE_ARGS)[0] == 1


def test_witness_needs_its_own_machine_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _witness_host(tmp_path, monkeypatch, own=INIT_PID_NS, entries={})

    def unreadable() -> None:
        raise OSError("machine-id not mounted")

    monkeypatch.setattr(witness, "current_lease_identity", unreadable)
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, "--self-check") == (
        1,
        "host_pid_visibility_unverified",
    )
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, *LEASE_ARGS) == (
        1,
        "lease_process_gone_not_proven",
    )


def test_witness_full_host_scan_proves_only_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = _witness_host(
        tmp_path, monkeypatch, own=INIT_PID_NS, entries={"77": LEASE_NS}
    )
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, "--self-check") == (
        0,
        "host_pid_visibility_verified",
    )
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, *LEASE_ARGS) == (
        1,
        "lease_process_gone_not_proven",
    )
    (proc / "77/ns/pid").unlink()
    (proc / "77/ns").rmdir()
    (proc / "77").rmdir()
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, *LEASE_ARGS) == (
        0,
        "lease_process_gone_proven",
    )
    other = list(LEASE_ARGS)
    other[1] = "f" * 32
    assert _cli(capsys, "--host-pid-ns", INIT_PID_NS, *other) == (
        1,
        "lease_process_gone_not_proven",
    )
