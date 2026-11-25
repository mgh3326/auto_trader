"""hk 6437: pi deploy.sh 의 성공 뒤 오래된 이미지 정리 (task 934 규칙 이식). 가짜 docker 만 쓴다.

가짜 docker 는 이미지 저장소(ID·태그·digest)와 컨테이너 표를 상태 파일에 두고, `image rm` 은 -f 를 거부하며
컨테이너가 쓰는 이미지를 지우지 않는다. 모든 호출은 기록하고 기록 시점의 deployed-digest 도 함께 남긴다.
배포 가짜(shiftgate·swap·게이트)는 tests/scripts/test_pi_shiftgate_deploy.py 의 것을 그대로 가져온다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.scripts.test_pi_shiftgate_deploy import (
    DIGEST_A,
    DIGEST_B,
    IMAGE,
    REF_A,
    REF_B,
    deploy,
    deploy_env,  # noqa: F401  (pytest fixture, fetched by name below)
    executable,
    marker,
    set_fleet,
)

PRUNE_PREFIX = "image prune:"


def _digest(ch: str) -> str:
    return f"{IMAGE}@sha256:{ch * 64}"


def _id(ch: str) -> str:
    return "sha256:" + ch * 64


# 상태: 현재(1)·이전(a)·컨테이너가 쓰는 것(4 실행 중, 8 정지, 9 생성만)은 남기고, 5·6·7·d 는 지운다.
# 2·e·f 는 다른 저장소가 섞였거나 접두만 같은 저장소라 건드리지 않고, 3 은 참조 없는 이미지라 건드리지 않는다.
PRUNE_STATE = {
    "images": {
        _id("1"): {
            "tags": [f"{IMAGE}:sha-1111111"],
            "digests": [_digest("1")],
        },  # 현재 digest
        _id("a"): {"tags": [], "digests": [_digest("a")]},  # 이전 digest (롤백 대상)
        _id("4"): {"tags": [], "digests": [_digest("4")]},  # 실행 중 컨테이너가 사용
        _id("8"): {"tags": [], "digests": [_digest("8")]},  # 정지된 컨테이너가 사용
        _id("9"): {
            "tags": [f"{IMAGE}:created-only"],
            "digests": [],
        },  # 생성만 된 컨테이너가 사용
        _id("5"): {
            "tags": [f"{IMAGE}:sha-5555555"],
            "digests": [_digest("5")],
        },  # 오래됨
        _id("6"): {"tags": [], "digests": [_digest("6")]},  # digest 만 있고 쓰이지 않음
        _id("7"): {
            "tags": [f"{IMAGE}:old", f"{IMAGE}:older"],
            "digests": [_digest("7")],
        },  # 여러 태그
        _id("d"): {
            "tags": [],
            "digests": [f"{IMAGE}@sha256:{'a' * 63}b"],
        },  # 이전 digest 와 한 자리 차이
        _id("f"): {
            "tags": ["ghcr.io/other/app:latest"],
            "digests": ["ghcr.io/other/app@sha256:" + "f" * 64],
        },
        _id("e"): {
            "tags": [],
            "digests": [f"{IMAGE}-dev@sha256:{'e' * 64}"],
        },  # 접두만 같은 저장소
        _id("2"): {
            "tags": [f"{IMAGE}:x", "other/app:y"],
            "digests": [],
        },  # 저장소가 섞임
        _id("3"): {"tags": [], "digests": []},  # 참조 없는 이미지
    },
    "containers": {
        "at-mcp-blue": {"image": _id("4"), "running": True, "config": _digest("4")},
        "at-api-green": {"image": _id("8"), "running": False, "config": _digest("8")},
        "at-mcp-next": {
            "image": _id("9"),
            "running": False,
            "config": f"{IMAGE}:created-only",
        },
    },
}
PRUNED = {_id(c) for c in "567d"}
KEPT = {_id(c) for c in "1a489"}
UNTOUCHED = {_id(c) for c in "2ef3"}

FAKE_PRUNE_DOCKER = r"""#!@PYTHON@ -I
import json, os, pathlib, sys

args = sys.argv[1:]
state_file = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
sg = pathlib.Path(os.environ["FAKE_SG"])
marker_file = pathlib.Path(os.environ["AT_RUN_DIR"]) / "deployed-digest"
faults = set(filter(None, os.environ.get("FAKE_FAULTS", "").split(",")))
state = json.loads(state_file.read_text())
marker_now = marker_file.read_text().strip() if marker_file.exists() else "-"
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "marker": marker_now}) + "\n")


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def image_id_of(ref):
    if ref in state["images"]:
        return ref
    for image_id, image in state["images"].items():
        if ref in image["tags"] or ref in image["digests"]:
            return image_id
    return None


def containers():
    rows = {name: dict(row) for name, row in state["containers"].items()}
    for single in sg.glob("single.*"):  # 싱글턴은 swap 가짜가 파일로 바꾼다
        ref = single.read_text().strip()
        rows[single.name[len("single."):]] = {
            "image": image_id_of(ref) or ref,
            "running": True,
            "config": ref,
        }
    return rows


cmd = args[0]
if cmd == "ps":
    if "ps" in faults:
        fail("Cannot connect to the Docker daemon")
    for name, row in containers().items():
        if "-a" in args or row["running"]:  # docker ps 는 -a 가 없으면 실행 중인 것만 보인다
            print("cid-" + name)
    sys.exit(0)
if cmd == "inspect":
    fmt, target = args[2], args[-1]
    name = target[len("cid-"):] if target.startswith("cid-") else target
    rows = containers()
    if name not in rows:
        fail("Error: No such object: " + target)
    row = rows[name]
    if "State.Running" in fmt:
        print("true" if row["running"] else "false")
    elif "Config.Image" in fmt:
        print(row["config"])
    elif fmt == "{{.Image}}":
        if "container_inspect" in faults:
            fail("Cannot connect to the Docker daemon")
        print(row["image"])
    else:
        fail("unsupported inspect format: " + fmt, 42)
    sys.exit(0)
if cmd == "image":
    sub = args[1]
    if sub == "inspect":
        if "image_inspect" in faults:
            fail("Cannot connect to the Docker daemon")
        fmt, ref = args[3], args[4]
        image_id = image_id_of(ref)
        if image_id is None:
            fail("Error: No such image: " + ref)
        image = state["images"][image_id]
        if fmt == "{{.Id}}":
            print(image_id)
        elif fmt.startswith("{{range .RepoTags}}"):
            for tag in image["tags"]:
                print(tag)
            for digest in image["digests"]:
                print(digest)
        else:
            fail("unsupported image format: " + fmt, 42)
        sys.exit(0)
    if sub == "ls":
        if "image_ls" in faults:
            fail("Cannot connect to the Docker daemon")
        for image_id, image in state["images"].items():
            for _ in range(max(1, len(image["tags"]))):  # 태그마다 한 행, 태그가 없으면 <none> 한 행
                print(image_id)
        sys.exit(0)
    if sub == "rm":
        if "image_rm" in faults:
            fail("Error response from daemon: injected failure")
        refs = args[2:]
        if "-f" in refs or "--force" in refs:
            fail("test: forced removal is never allowed", 99)
        for ref in refs:
            image_id = image_id_of(ref)
            if image_id is None:
                fail("Error: No such image: " + ref)
            users = [name for name, row in containers().items() if row["image"] == image_id]
            if users:
                fail("conflict: unable to remove " + ref + " - image is being used by " + users[0])
            image = state["images"][image_id]
            if ref in image["tags"]:
                image["tags"].remove(ref)
            elif ref in image["digests"]:
                image["digests"].remove(ref)
            if not image["tags"] and not image["digests"]:
                del state["images"][image_id]
        state_file.write_text(json.dumps(state))
        sys.exit(0)
    fail("unsupported image command: " + sub, 42)
fail("unsupported docker command: " + cmd, 42)
"""


@pytest.fixture
def prune_env(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    base: dict = request.getfixturevalue("deploy_env")
    state_file = tmp_path / "docker-state.json"
    state_file.write_text(json.dumps(PRUNE_STATE))
    log = tmp_path / "docker.log"
    log.write_text("")
    executable(
        base["tmp"] / "bin" / "docker",
        FAKE_PRUNE_DOCKER.replace("@PYTHON@", sys.executable),
    )
    env = {
        **base,
        "env": {
            **base["env"],
            "FAKE_DOCKER_STATE": str(state_file),
            "FAKE_DOCKER_LOG": str(log),
        },
    }
    env["docker_state"] = state_file
    env["docker_log"] = log
    # 기본 상태: 이전 배포 A 가 기록되어 있고 전 컴포넌트가 A. 배포하면 B 가 되고 A 가 이전 digest 가 된다.
    set_fleet(
        env,
        api=(DIGEST_A, "none"),
        mcp=(DIGEST_A, "none"),
        singletons=REF_A,
        marker=REF_A,
    )
    return env


def docker_calls(env: dict) -> list[dict]:
    return [
        json.loads(line) for line in env["docker_log"].read_text().splitlines() if line
    ]


def image_calls(env: dict) -> list[dict]:
    return [c for c in docker_calls(env) if c["args"][0] == "image"]


def rm_calls(env: dict) -> list[list[str]]:
    return [c["args"] for c in image_calls(env) if c["args"][1] == "rm"]


def probe_calls(env: dict) -> list[dict]:
    """정리가 하는 읽기: docker ps 와 docker image 조회. 배포 자체는 ps 를 실패 시에만 부른다."""
    return [c for c in docker_calls(env) if c["args"][0] in ("ps", "image")]


def ids_on(lines: list[str], verb: str) -> set[str]:
    prefix = f"{PRUNE_PREFIX} {verb} "
    return {line.split()[3] for line in lines if line.startswith(prefix)}


def after_deploy_ok(stdout: str) -> list[str]:
    lines = stdout.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("deploy OK:"))
    return lines[index + 1 :]


def state_images(env: dict) -> set[str]:
    return set(json.loads(env["docker_state"].read_text())["images"])


def test_success_removes_only_unkept_owned_images(prune_env: dict) -> None:
    result = deploy(prune_env, "main")
    assert result.returncode == 0, result.stderr
    assert "WARNING" not in result.stderr
    assert ids_on(result.stdout.splitlines(), "removed") == PRUNED
    assert ids_on(result.stdout.splitlines(), "kept") == KEPT
    assert state_images(prune_env) == KEPT | UNTOUCHED
    assert marker(prune_env) == REF_B
    assert marker(prune_env, "deployed-digest.previous") == REF_A


def test_rm_of_a_multi_tag_image_names_every_reference_and_never_forces(
    prune_env: dict,
) -> None:
    result = deploy(prune_env, "main")
    assert result.returncode == 0, result.stderr
    calls = rm_calls(prune_env)
    assert all("-f" not in args and "--force" not in args for args in calls)
    multi = [args for args in calls if f"{IMAGE}:old" in args]
    assert multi == [["image", "rm", f"{IMAGE}:old", f"{IMAGE}:older", _digest("7")]]


def test_after_deploy_ok_only_one_line_per_image_follows(prune_env: dict) -> None:
    result = deploy(prune_env, "main")
    assert result.returncode == 0, result.stderr
    tail = after_deploy_ok(result.stdout)
    assert len(tail) == len(PRUNED) + len(KEPT)
    assert all(line.startswith(f"{PRUNE_PREFIX} ") for line in tail)
    assert ids_on(tail, "removed") == PRUNED
    assert ids_on(tail, "kept") == KEPT


def test_docker_calls_are_read_only_probes_then_by_reference_removal_after_the_marker(
    prune_env: dict,
) -> None:
    result = deploy(prune_env, "main")
    assert result.returncode == 0, result.stderr
    assert {c["args"][0] for c in docker_calls(prune_env)} <= {"inspect", "ps", "image"}
    assert {c["args"][1] for c in image_calls(prune_env)} <= {"inspect", "ls", "rm"}
    assert probe_calls(prune_env)
    assert all(c["marker"] == REF_B for c in probe_calls(prune_env))


@pytest.mark.parametrize(("value", "warns"), [("0", False), ("", True), ("yes", True)])
def test_kill_switch_zero_or_invalid_removes_nothing(
    prune_env: dict, value: str, warns: bool
) -> None:
    result = deploy(prune_env, "main", AT_IMAGE_PRUNE_ENABLED=value)
    assert result.returncode == 0, result.stderr
    assert probe_calls(prune_env) == []
    assert rm_calls(prune_env) == []
    assert ("WARNING" in result.stderr) is warns
    assert state_images(prune_env) == set(PRUNE_STATE["images"])


@pytest.mark.parametrize(
    "extra",
    [{"FAKE_SG_FAIL": "at-api"}, {"FAKE_SG_FAIL": "at-mcp"}, {"FAKE_SWAP_RC": "1"}],
)
def test_failed_deploy_runs_no_prune(prune_env: dict, extra: dict) -> None:
    result = deploy(prune_env, "main", **extra)
    assert result.returncode != 0
    assert "deploy FAILED" in result.stderr
    assert image_calls(prune_env) == []
    assert rm_calls(prune_env) == []
    assert state_images(prune_env) == set(PRUNE_STATE["images"])


def test_rollback_runs_no_prune(prune_env: dict) -> None:
    set_fleet(
        prune_env,
        api=(DIGEST_B, DIGEST_A),
        mcp=(DIGEST_B, DIGEST_A),
        singletons=REF_B,
        marker=REF_B,
        previous_marker=REF_A,
    )
    result = deploy(prune_env, "--rollback")
    assert result.returncode == 0, result.stderr
    assert marker(prune_env) == REF_A
    assert probe_calls(prune_env) == []
    assert rm_calls(prune_env) == []


def test_fully_deployed_release_runs_no_prune(prune_env: dict) -> None:
    set_fleet(
        prune_env,
        api=(DIGEST_B, DIGEST_A),
        mcp=(DIGEST_B, DIGEST_A),
        singletons=REF_B,
        marker=REF_B,
        previous_marker=REF_A,
    )
    result = deploy(prune_env, "main")
    assert result.returncode == 0, result.stderr
    assert probe_calls(prune_env) == []
    assert rm_calls(prune_env) == []


@pytest.mark.parametrize(
    "previous_text",
    [
        None,  # 기록이 없다(처음 배포)
        REF_A,  # 개행이 없다
        REF_A + "\nextra\n",  # 두 줄이다
        REF_A + "\r\n",  # CR 이 붙었다
        REF_A + "\n\x00",  # 숨은 NUL 바이트
        "not-a-digest\n",
    ],
)
def test_unreadable_previous_record_removes_nothing(
    prune_env: dict, previous_text
) -> None:
    set_fleet(
        prune_env,
        api=(DIGEST_A, "none"),
        mcp=(DIGEST_A, "none"),
        singletons=REF_A,
        marker=None,
        previous_marker=None,
    )
    if previous_text is not None:
        (prune_env["run"] / "deployed-digest.previous").write_text(previous_text)
    result = deploy(prune_env, "main")
    assert result.returncode == 0, result.stderr
    assert "WARNING: image prune skipped" in result.stderr
    assert probe_calls(prune_env) == []
    assert rm_calls(prune_env) == []
    assert state_images(prune_env) == set(PRUNE_STATE["images"])


@pytest.mark.parametrize(
    "fault", ["ps", "image_ls", "image_inspect", "container_inspect"]
)
def test_unreadable_docker_state_keeps_every_image(prune_env: dict, fault: str) -> None:
    result = deploy(prune_env, "main", FAKE_FAULTS=fault)
    assert result.returncode == 0, result.stderr
    assert "deploy OK" in result.stdout
    assert "WARNING: image prune skipped" in result.stderr
    assert rm_calls(prune_env) == []
    assert not any(
        line.startswith(f"{PRUNE_PREFIX} removed")
        for line in result.stdout.splitlines()
    )
    assert state_images(prune_env) == set(PRUNE_STATE["images"])


def test_a_failed_removal_is_reported_and_the_deploy_still_succeeds(
    prune_env: dict,
) -> None:
    result = deploy(prune_env, "main", FAKE_FAULTS="image_rm")
    assert result.returncode == 0, result.stderr
    assert "deploy OK" in result.stdout
    assert "WARNING: image prune could not remove" in result.stderr
    assert "WARNING: image prune incomplete" in result.stderr
    assert "WARNING: image prune did not complete" in result.stderr
    assert ids_on(result.stdout.splitlines(), "removed") == set()
    assert marker(prune_env) == REF_B
