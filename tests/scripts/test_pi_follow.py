"""hk 6037: deploy/shiftgate/pi/follow.sh — the pi follows the `deploy` tag, but trusts nobody.

Everything is fake: the registry, the gate, deploy.sh, systemctl and (in the end-to-end tests)
api.github.com. No docker, no network, no pi.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PI = REPO / "deploy" / "shiftgate" / "pi"
FOLLOW = PI / "follow.sh"
SERVICE = PI / "at-follow.service"
TIMER = PI / "at-follow.timer"
IMAGE = "ghcr.io/mgh3326/auto_trader"
SHA_A, SHA_B = "a" * 40, "b" * 40
HEX_A, HEX_B = "a" * 64, "b" * 64
REF_A, REF_B = f"{IMAGE}@sha256:{HEX_A}", f"{IMAGE}@sha256:{HEX_B}"

# --- fakes --------------------------------------------------------------------------------------

FAKE_GATE = r"""
import json, os, sys
d = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
log = open(os.path.join(d, "gate.log"), "a")
log.write(json.dumps({"argv": argv, "github": os.environ.get("AT_DEPLOY_GATE_GITHUB")}) + "\n")
log.close()
image = argv[argv.index("--image") + 1]
tag = image.rsplit(":", 1)[1]
if "--resolve-only" in argv:
    path = os.path.join(d, "tag." + tag)
    if not os.path.exists(path):
        sys.stderr.write("deploy gate: REFUSED: registry unavailable\n")
        sys.exit(78)
    sys.stdout.write(open(path).read())
    sys.exit(0)
verdict = open(os.path.join(d, "verdict")).read().strip()
if verdict == "ok":
    pinned = open(os.path.join(d, "tag." + tag)).read().split("\t")[0]
    override = os.path.join(d, "verify_digest")
    if os.path.exists(override):
        pinned = open(override).read().strip()
    print(pinned)
    sys.exit(0)
sys.stderr.write("deploy gate: " + verdict + "\n")
sys.exit(78)
"""

FAKE_DEPLOY = r"""#!/usr/bin/env bash
d="$FAKE_DIR"
printf 'deploy %s|github=%s|lock=%s|verified=%s\n' "$*" "${AT_DEPLOY_GATE_GITHUB:-}" "${AT_DEPLOY_LOCK_HELD:-}" "${AT_FOLLOW_VERIFIED_DIGEST:-}" >>"$d/calls"
rc="$(cat "$d/deploy_rc" 2>/dev/null || echo 0)"
if [[ "$rc" == 0 && ! -e "$d/deploy_skips_marker" ]]; then
  value="$(cat "$d/marker_value" 2>/dev/null || cut -f1 "$d/tag.deploy")"
  printf '%s\n' "$value" >"$AT_RUN_DIR/deployed-digest"
fi
cat "$d/deploy_out" 2>/dev/null || echo "fake deploy output"
exit "$rc"
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# records every call: follow.sh must never reach it (the DB backup is a Dagu job now, hk 6466)
printf 'systemctl %s\n' "$*" >>"$FAKE_DIR/calls"
exit 0
"""

FAKE_PANEWIRE = r"""#!/usr/bin/env bash
printf 'panewire %s\n' "$*" >>"$FAKE_DIR/calls"
exit "$(cat "$FAKE_DIR/panewire_rc" 2>/dev/null || echo 0)"
"""


def executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path: Path) -> dict:
    fake = tmp_path / "fake"
    fake.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (fake / "calls").write_text("")
    (fake / "verdict").write_text("ok")
    gate = tmp_path / "gate.py"
    gate.write_text(FAKE_GATE)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "AT_RUN_DIR": str(run),
        "FAKE_DIR": str(fake),
        "AT_DEPLOY_GATE_PYTHON": sys.executable,
        "AT_DEPLOY_GATE_SCRIPT": str(gate),
        "AT_FOLLOW_DEPLOY_SH": str(executable(tmp_path / "deploy.sh", FAKE_DEPLOY)),
        "AT_SYSTEMCTL_BIN": str(executable(tmp_path / "systemctl", FAKE_SYSTEMCTL)),
    }
    state = {"env": env, "fake": fake, "run": run, "bin": bin_dir, "tmp": tmp_path}
    set_tags(state, deploy=(REF_B, SHA_B), sha=(REF_B, SHA_B))
    set_deployed(state, REF_A)
    return state


def set_tags(
    state: dict, *, deploy: tuple[str, str], sha: tuple[str, str] | None
) -> None:
    (state["fake"] / "tag.deploy").write_text(f"{deploy[0]}\t{deploy[1]}\n")
    short = deploy[1][:7]
    target = state["fake"] / f"tag.sha-{short}"
    if sha is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(f"{sha[0]}\t{sha[1]}\n")


def set_deployed(state: dict, ref: str | None) -> None:
    marker = state["run"] / "deployed-digest"
    marker.unlink(missing_ok=True)
    if ref:
        marker.write_text(ref + "\n")


def follow(state: dict, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FOLLOW), *args],
        env={**state["env"], **extra},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def calls(state: dict) -> list[str]:
    return (state["fake"] / "calls").read_text().splitlines()


def gate_log(state: dict) -> list[dict]:
    path = state["fake"] / "gate.log"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def verifications(state: dict) -> list[dict]:
    return [row for row in gate_log(state) if "--resolve-only" not in row["argv"]]


def status(state: dict) -> dict:
    return json.loads((state["run"] / "follow-status.json").read_text())


def hold_line(state: dict) -> list[str]:
    return (state["run"] / "follow-hold").read_text().rstrip("\n").split("\t")


# --- the happy path and the quiet no-ops --------------------------------------------------------


def test_a_new_verified_digest_is_installed_by_deploy_sh_with_the_sha_tag(
    host: dict,
) -> None:
    result = follow(host)
    assert result.returncode == 0, result.stderr
    steps = calls(host)
    assert [s.split("|")[0] for s in steps] == [f"deploy sha-{SHA_B[:7]}"]
    deploy_call = steps[0]
    assert "--allow-unverified" not in deploy_call  # the timer never waives the gate
    assert (
        "github=anonymous" in deploy_call
    )  # deploy.sh's own gate needs no gh on the pi
    assert re.search(
        r"lock=\d+", deploy_call
    )  # follow holds the host lock and deploy.sh inherits it
    assert (host["run"] / "deployed-digest").read_text().strip() == REF_B
    assert status(host)["result"] == "deployed" and status(host)["sha"] == SHA_B
    assert not (host["run"] / "follow-hold").exists()
    assert not (host["run"] / "shiftgate-deploy.lock").exists()  # the lock is released


def test_an_install_never_runs_a_db_backup_first(host: dict) -> None:
    # hk 6466: the DB backup is the daily Dagu job at-pg-backup.yaml, not a step before each install.
    assert follow(host).returncode == 0
    assert systemctl_calls(host) == []
    assert status(host)["result"] == "deployed"


def test_the_independent_check_asks_for_main_ancestry_and_the_exact_sha_anonymously(
    host: dict,
) -> None:
    follow(host)
    (check,) = verifications(host)
    assert {"--main-ancestor", "--expect-sha", SHA_B}.issubset(check["argv"])
    assert check["argv"][check["argv"].index("--image") + 1] == f"{IMAGE}:deploy"
    assert check["github"] == "anonymous"


def test_the_same_digest_twice_is_a_quiet_no_op_that_never_reads_github(
    host: dict,
) -> None:
    assert follow(host).returncode == 0
    host["fake"].joinpath("calls").write_text("")
    (host["fake"] / "gate.log").unlink()
    again = follow(host)
    assert again.returncode == 0 and again.stdout == "" and again.stderr == ""
    assert calls(host) == []
    assert verifications(host) == []  # only the registry resolve ran


def test_a_digest_already_installed_by_hand_is_not_installed_again(host: dict) -> None:
    set_deployed(host, REF_B)
    result = follow(host)
    assert result.returncode == 0 and result.stderr == ""
    assert calls(host) == [] and verifications(host) == []


# --- invariant: only a main-ancestor SHA with a green full suite is installed --------------------


@pytest.mark.parametrize(
    "verdict",
    [
        "main-ancestor: bbbb is not an ancestor of main (compare diverged)",
        'test (3.13, 2): not successful ["completed", "failure"]',
        "lint: missing",
    ],
)
def test_a_refused_sha_installs_nothing_and_is_held(host: dict, verdict: str) -> None:
    (host["fake"] / "verdict").write_text(verdict)
    result = follow(host)
    assert result.returncode == 78
    assert calls(host) == []  # no install
    assert (host["run"] / "deployed-digest").read_text().strip() == REF_A
    assert status(host)["result"] == "refused"
    digest, _created, retry_at, reason = hold_line(host)
    assert digest == REF_B and retry_at == "0" and "gate refused" in reason


def test_a_hand_moved_tag_onto_an_unverified_image_is_refused_even_if_the_tag_looks_legit(
    host: dict,
) -> None:
    # Someone moved `deploy` onto a digest whose commit never passed CI: the gate (not the tag) decides.
    set_tags(host, deploy=(REF_B, SHA_B), sha=(REF_B, SHA_B))
    (host["fake"] / "verdict").write_text(
        "lint: missing (no exact-SHA push/dispatch Test run)"
    )
    assert follow(host).returncode == 78
    assert calls(host) == []


def test_a_tag_that_moves_between_resolve_and_verify_is_refused(host: dict) -> None:
    (host["fake"] / "verify_digest").write_text(
        REF_A
    )  # the gate saw a different digest
    result = follow(host)
    assert result.returncode == 78 and calls(host) == []
    assert "moved during verification" in status(host)["detail"]


def test_the_sha_tag_must_be_the_verified_digest(host: dict) -> None:
    set_tags(
        host, deploy=(REF_B, SHA_B), sha=(REF_A, SHA_B)
    )  # sha-<7> points elsewhere
    assert follow(host).returncode == 78
    assert calls(host) == []


# --- invariant: a failed digest is not retried every tick ---------------------------------------


def test_a_held_digest_is_skipped_quietly_until_a_new_digest_appears(
    host: dict,
) -> None:
    (host["fake"] / "verdict").write_text("lint: missing")
    assert follow(host).returncode == 78
    n = len(verifications(host))
    quiet = follow(host)
    assert (
        quiet.returncode == 0 and quiet.stderr == "" and len(verifications(host)) == n
    )
    # a new digest appears and passes: the old hold does not get in the way
    (host["fake"] / "verdict").write_text("ok")
    set_tags(
        host,
        deploy=(f"{IMAGE}@sha256:{'c' * 64}", "c" * 40),
        sha=(f"{IMAGE}@sha256:{'c' * 64}", "c" * 40),
    )
    assert follow(host).returncode == 0
    assert any(c.startswith("deploy sha-ccccccc") for c in calls(host))


def test_evidence_that_was_merely_unavailable_is_retried_after_the_wait(
    host: dict,
) -> None:
    (host["fake"] / "verdict").write_text(
        "main-ancestor: unverified (GitHub evidence unavailable (HTTP 403))"
    )
    assert follow(host).returncode == 78
    digest, created, retry_at, reason = hold_line(host)
    assert int(retry_at) > int(created) and "evidence unavailable" in reason
    n = len(verifications(host))
    assert (
        follow(host).returncode == 0 and len(verifications(host)) == n
    )  # inside the wait: skipped
    # the wait is over and GitHub answers now
    (host["run"] / "follow-hold").write_text(f"{digest}\t{created}\t1\t{reason}\n")
    (host["fake"] / "verdict").write_text("ok")
    assert follow(host).returncode == 0
    assert any(c.startswith(f"deploy sha-{SHA_B[:7]}") for c in calls(host))


def test_a_failed_install_is_held_and_not_retried_on_the_next_tick(host: dict) -> None:
    (host["fake"] / "deploy_rc").write_text("3")
    result = follow(host)
    assert result.returncode == 3
    assert status(host)["result"] == "failed"
    assert hold_line(host)[0] == REF_B and hold_line(host)[2] == "0"
    host["fake"].joinpath("calls").write_text("")
    assert follow(host).returncode == 0 and calls(host) == []


def test_a_rate_limited_gate_inside_deploy_sh_is_retried_later_not_held_forever(
    host: dict,
) -> None:
    (host["fake"] / "deploy_rc").write_text("78")
    (host["fake"] / "deploy_out").write_text(
        "deploy gate: test (3.13, 1): unverified (GitHub evidence unavailable (HTTP 403))"
    )
    result = follow(host)
    assert result.returncode == 78
    digest, created, retry_at, reason = hold_line(host)
    assert digest == REF_B and int(retry_at) > int(
        created
    )  # retry-after, not permanent
    assert (host["run"] / "deployed-digest").read_text().strip() == REF_A
    # a real refusal from deploy.sh's gate stays permanent
    (host["run"] / "follow-hold").unlink()
    (host["fake"] / "deploy_out").write_text(
        "deploy gate: test (3.13, 1): not successful"
    )
    assert follow(host).returncode == 78
    assert hold_line(host)[2] == "0"


def deploy_gate_unavailable(host: dict) -> None:
    (host["fake"] / "deploy_rc").write_text("78")
    (host["fake"] / "deploy_out").write_text(
        "deploy gate: test (3.13, 1): unverified (GitHub evidence unavailable (HTTP 403))"
    )


def expire_hold(host: dict) -> None:
    digest, created, _retry, reason = hold_line(host)
    (host["run"] / "follow-hold").write_text(f"{digest}\t{created}\t1\t{reason}\n")


def systemctl_calls(host: dict) -> list[str]:
    return [c for c in calls(host) if c.startswith("systemctl")]


def test_follow_hands_deploy_sh_the_digest_it_just_verified(host: dict) -> None:
    assert follow(host).returncode == 0
    deploy_call = next(c for c in calls(host) if c.startswith("deploy "))
    assert deploy_call.endswith(f"|verified={REF_B}")  # no second GitHub round for it


def test_a_retry_after_a_transient_deploy_sh_failure_only_runs_deploy_sh_again(
    host: dict,
) -> None:
    deploy_gate_unavailable(host)
    assert follow(host).returncode == 78
    expire_hold(host)
    assert follow(host).returncode == 78  # deploy.sh still cannot read GitHub
    expire_hold(host)
    (host["fake"] / "deploy_rc").write_text("0")
    assert follow(host).returncode == 0
    assert [c.split("|")[0] for c in calls(host)] == [f"deploy sha-{SHA_B[:7]}"] * 3
    assert systemctl_calls(host) == []


def test_transient_deploy_sh_failures_are_capped_per_digest_then_held_for_good(
    host: dict,
) -> None:
    deploy_gate_unavailable(host)
    for attempt in (1, 2, 3):  # three retries are allowed
        assert follow(host).returncode == 78
        assert int(hold_line(host)[2]) > 0, attempt
        expire_hold(host)
    assert follow(host).returncode == 78  # the fourth failure in a row
    assert hold_line(host)[2] == "0" and "retries exhausted" in hold_line(host)[3]
    n = len(calls(host))
    assert follow(host).returncode == 0 and len(calls(host)) == n  # quiet from now on
    assert systemctl_calls(host) == []


def test_transient_evidence_failures_of_the_followers_own_gate_are_capped_too(
    host: dict,
) -> None:
    (host["fake"] / "verdict").write_text(
        "main-ancestor: unverified (GitHub evidence unavailable (HTTP 403))"
    )
    for _ in range(3):
        assert follow(host).returncode == 78
        expire_hold(host)
    assert follow(host).returncode == 78
    assert hold_line(host)[2] == "0"
    assert systemctl_calls(host) == []


def test_a_permanent_deploy_sh_refusal_that_mentions_a_registry_is_held_not_retried(
    host: dict,
) -> None:
    (host["fake"] / "deploy_rc").write_text("78")
    (host["fake"] / "deploy_out").write_text(
        "deploy refused: image registry digest does not match the inspect result"
    )
    assert follow(host).returncode == 78
    assert hold_line(host)[2] == "0"  # permanent: no retry-after


def test_a_permanent_gate_refusal_that_mentions_a_registry_is_held_not_retried(
    host: dict,
) -> None:
    (host["fake"] / "verdict").write_text("REFUSED: registry image is not from main")
    assert follow(host).returncode == 78
    assert hold_line(host)[2] == "0"


def test_exit_zero_without_the_marker_is_a_failure(host: dict) -> None:
    (host["fake"] / "deploy_skips_marker").write_text("")
    assert follow(host).returncode == 1
    assert (
        status(host)["result"] == "failed"
        and "deployed-digest" in status(host)["detail"]
    )


# --- the host lock -------------------------------------------------------------------------------


def test_a_busy_host_lock_is_exit_75_without_a_backup_or_install(host: dict) -> None:
    holder = subprocess.Popen(["sleep", "60"])
    try:
        lock = host["run"] / "shiftgate-deploy.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{holder.pid}\n")
        result = follow(host)
        assert result.returncode == 75
        assert calls(host) == [] and verifications(host) == []
        assert not (
            host["run"] / "follow-hold"
        ).exists()  # busy is not a failure of the digest
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=10)


def test_the_no_op_tick_never_contends_for_the_host_lock(host: dict) -> None:
    # while a manual deploy holds the lock, ticks for an already-installed digest stay silent (not 75)
    set_deployed(host, REF_B)
    holder = subprocess.Popen(["sleep", "60"])
    try:
        lock = host["run"] / "shiftgate-deploy.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{holder.pid}\n")
        result = follow(host)
        assert result.returncode == 0 and result.stderr == ""
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=10)


def test_a_dead_owners_lock_is_reclaimed(host: dict) -> None:
    lock = host["run"] / "shiftgate-deploy.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999\n")
    assert follow(host).returncode == 0


def test_a_deployment_that_finished_while_waiting_for_the_lock_is_not_repeated(
    host: dict,
) -> None:
    # simulated: marker already matches by the time the lock is held
    set_deployed(host, REF_B)
    assert follow(host).returncode == 0 and calls(host) == []


# --- reporting never decides the deploy ---------------------------------------------------------


def test_panewire_is_used_only_when_present_and_asked_for_and_never_fails_the_deploy(
    host: dict,
) -> None:
    follow(host)
    assert not any(c.startswith("panewire") for c in calls(host))  # no lane configured
    set_deployed(host, REF_A)
    executable(host["bin"] / "panewire", FAKE_PANEWIRE)
    (host["fake"] / "panewire_rc").write_text("1")  # the notifier is broken
    result = follow(host, AT_FOLLOW_PANEWIRE_LANE="design")
    assert result.returncode == 0
    assert any(
        c.startswith("panewire emit --kind lane.event --lane design --type info")
        for c in calls(host)
    )
    assert status(host)["result"] == "deployed"


def test_an_unwritable_log_does_not_stop_a_deploy(host: dict) -> None:
    (host["run"] / "follow.log").mkdir()  # a directory where the log file should be
    assert follow(host).returncode == 0
    assert (host["run"] / "deployed-digest").read_text().strip() == REF_B


def test_an_unreadable_registry_changes_nothing_and_is_not_held(host: dict) -> None:
    (host["fake"] / "tag.deploy").unlink()
    result = follow(host)
    assert result.returncode == 1 and "cannot resolve" in result.stderr
    assert calls(host) == [] and not (host["run"] / "follow-hold").exists()


# --- operator commands: hold for rollback, clear, status ----------------------------------------


def test_hold_pins_the_current_tag_digest_so_a_rollback_is_not_undone(
    host: dict,
) -> None:
    # B was installed, turned out bad and was rolled back: the marker is A again, the tag still says B.
    set_deployed(host, REF_A)
    assert follow(host, "--hold", "rolled back: bad release").returncode == 0
    assert hold_line(host)[0] == REF_B
    assert (
        follow(host).returncode == 0 and calls(host) == []
    )  # the timer does not reinstall B
    assert "hold" in follow(host, "--status").stdout
    assert follow(host, "--clear-hold").returncode == 0
    assert not (host["run"] / "follow-hold").exists()
    assert follow(host).returncode == 0 and any(
        c.startswith("deploy sha-") for c in calls(host)
    )


def test_usage_errors_touch_nothing(host: dict) -> None:
    for args in (["--bogus"], ["--hold"], ["x"]):
        assert follow(host, *args).returncode == 2
    assert follow(host, "--help").returncode == 0
    assert calls(host) == [] and gate_log(host) == []


def test_a_missing_gate_refuses_before_any_registry_or_install_step(host: dict) -> None:
    result = follow(host, AT_DEPLOY_GATE_SCRIPT=str(host["tmp"] / "nope.py"))
    assert result.returncode == 78 and calls(host) == []


# --- the real gate end to end: fake registry + fake api.github.com -------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
# buildx imagetools inspect <ref> --format '{{json .Manifest}}' | '{{json .Image}}'
ref="$4"
for arg do fmt="$arg"; done
case "$fmt" in
  *.Manifest*)
    case "$ref" in
      *:deploy|*:sha-*) printf '{"digest":"sha256:%s"}\n' "$(cat "$FAKE_DIR/image_hex")" ;;
      *) printf '{"digest":"%s"}\n' "${ref#*@}" ;;
    esac ;;
  *.Image*) printf '{"config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}\n' "$(cat "$FAKE_DIR/image_sha")" ;;
  *) exit 42 ;;
esac
"""

SITECUSTOMIZE = r"""
import io, json, os, urllib.request

routes = json.load(open(os.path.join(os.environ["FAKE_DIR"], "github_routes.json")))
log = os.path.join(os.environ["FAKE_DIR"], "github.log")


class Response(io.BytesIO):
    headers = {"Link": ""}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class Opener:
    def open(self, request, timeout=0):
        url = request.full_url
        open(log, "a").write(url + "|" + ",".join(sorted(k.lower() for k in request.headers)) + "\n")
        for needle, body in routes.items():
            if needle in url:
                return Response(json.dumps(body).encode())
        raise AssertionError("unexpected GitHub request " + url)


urllib.request.build_opener = lambda *a, **k: Opener()
"""


def real_gate_host(
    host: dict, *, revision: str, compare: str, jobs_ok: bool = True
) -> None:
    fake = host["fake"]
    (fake / "image_hex").write_text("b" * 64)
    (fake / "image_sha").write_text(revision)
    executable(host["bin"] / "docker", FAKE_DOCKER)
    names = ["lint", "taskiq-smoke", *(f"test (3.13, {n})" for n in range(1, 5))]
    jobs = [
        {
            "id": i,
            "name": n,
            "status": "completed",
            "conclusion": "success" if (jobs_ok or i != 3) else "failure",
        }
        for i, n in enumerate(names, 1)
    ]
    routes = {
        "/actions/workflows/test.yml/runs?": {
            "workflow_runs": [
                {"id": 100, "run_attempt": 1, "head_sha": revision, "event": "push"}
            ]
        },
        "/actions/runs/100/attempts/1/jobs?": {"jobs": jobs},
        "/compare/": {"status": compare},
    }
    (fake / "github_routes.json").write_text(json.dumps(routes))
    site = host["tmp"] / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    host["env"]["PYTHONPATH"] = str(site)
    host["env"].pop("AT_DEPLOY_GATE_SCRIPT")
    host["env"]["AT_DEPLOY_GATE_SCRIPT"] = str(REPO / "scripts" / "deploy_ci_gate.py")
    # the sha-<7> tag resolves through the same fake docker
    (fake / "tag.deploy").unlink(missing_ok=True)
    (fake / "marker_value").write_text(REF_B)


def github_requests(host: dict) -> list[str]:
    path = host["fake"] / "github.log"
    return path.read_text().splitlines() if path.exists() else []


def test_end_to_end_a_green_main_commit_is_installed_with_no_gh_and_no_credentials(
    host: dict,
) -> None:
    real_gate_host(host, revision=SHA_B, compare="ahead")
    result = follow(host)
    assert result.returncode == 0, result.stderr
    assert any(c.startswith(f"deploy sha-{SHA_B[:7]}") for c in calls(host))
    requests = github_requests(host)
    assert requests and all(
        r.startswith("https://api.github.com/repos/mgh3326/auto_trader/")
        for r in requests
    )
    assert all("authorization" not in r.split("|")[1] for r in requests)


def test_end_to_end_a_hand_moved_tag_onto_a_commit_that_is_not_on_main_installs_nothing(
    host: dict,
) -> None:
    real_gate_host(host, revision=SHA_B, compare="diverged")
    result = follow(host)
    assert result.returncode == 78
    assert calls(host) == []
    assert (
        "not an ancestor of main" in result.stderr
        or "not an ancestor of main" in status(host)["detail"]
    )
    assert (host["run"] / "deployed-digest").read_text().strip() == REF_A


def test_end_to_end_a_hand_moved_tag_onto_a_red_commit_installs_nothing(
    host: dict,
) -> None:
    real_gate_host(host, revision=SHA_B, compare="ahead", jobs_ok=False)
    result = follow(host)
    assert result.returncode == 78
    assert calls(host) == []
    assert "test (3.13, 1)" in status(host)["detail"]


def test_end_to_end_github_being_unreachable_never_installs(host: dict) -> None:
    real_gate_host(host, revision=SHA_B, compare="ahead")
    (host["fake"] / "github_routes.json").write_text(
        "{}"
    )  # every request is an unexpected one
    result = follow(host)
    assert result.returncode == 78 and calls(host) == []


# --- static shape ---------------------------------------------------------------------------------


def test_follow_is_valid_executable_bash_and_never_waives_the_gate() -> None:
    assert FOLLOW.stat().st_mode & 0o100
    assert subprocess.run(["bash", "-n", str(FOLLOW)], check=False).returncode == 0
    body = FOLLOW.read_text()
    code = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    text = "\n".join(code)
    assert "--allow-unverified" not in text
    assert "shiftgate follow" not in text and "--heal" not in text
    assert "--main-ancestor" in text and "AT_DEPLOY_GATE_GITHUB=anonymous" in text
    assert (
        "ssh " not in text and "scp " not in text
    )  # nothing reaches into the pi, nothing reaches out


def test_the_units_run_follow_from_the_installed_path_and_treat_75_as_success() -> None:
    service, timer = SERVICE.read_text(), TIMER.read_text()
    assert "ExecStart=/root/at-run/shiftgate-pi/follow.sh" in service
    assert "SuccessExitStatus=75" in service and "Type=oneshot" in service
    assert "Restart=" not in service  # a failed install is held, not restarted
    assert "OnUnitInactiveSec=60s" in timer and "OnBootSec=" in timer
    assert "Unit=at-follow.service" in timer and "WantedBy=timers.target" in timer


def test_follow_help_lists_only_its_real_flags() -> None:
    usage = subprocess.run(
        ["bash", str(FOLLOW), "--help"], capture_output=True, text=True, check=False
    )
    assert usage.returncode == 0
    assert set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", usage.stderr)) == {
        "--hold",
        "--clear-hold",
        "--status",
    }
