"""Task 934: post-success image prune, using only a stateful fake docker.

The fake keeps a daemon-like image store (IDs, tags, repo digests, sizes) and a
container table (image ID, running flag). `docker image ls` lists one row per
tag like the real CLI, `docker image rm` untags references and refuses to drop
the last reference of an image any container uses (no -f), and every call is
logged so tests can assert what the script attempted, not only the end state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = Path(os.environ.get("DEPLOY_UNDER_TEST", REPO / "scripts/deploy-ncp-pull.sh"))
CONTRACT = REPO / "docs/contracts/task-934-image-prune.md"
REPOSITORY = "ghcr.io/mgh3326/auto_trader"


def _digest(ch: str) -> str:
    return f"{REPOSITORY}@sha256:" + ch * 64


def _id(ch: str) -> str:
    return "sha256:" + ch * 64


OLD, OLD_ID = _digest("1"), _id("a")  # what the units run before the deploy
NEW, NEW_ID = _digest("2"), _id("b")  # what `:main` resolves to on pull
KIS, KIS_ID = _digest("3"), _id("c")  # skipped KIS unit's retained digest
PREVX, PREVX_ID = _digest("4"), _id("d")  # recorded rollback target, unused
STALE, STALE_ID = _digest("5"), _id("e")  # tagged, unused
DANGLING, DANGLING_ID = _digest("6"), _id("f")  # digest only, unused
MULTI, MULTI_ID = _digest("7"), _id("0")  # several tags, unused
STOPPED, STOPPED_ID = _digest("8"), _id("9")  # used only by a stopped container
# One hex digit away from NEW: equality must not treat it as the kept digest.
NEAR = f"{REPOSITORY}@sha256:" + "2" * 63 + "3"
NEAR_ID = _id("8")
DEV_ID, BACKUP_ID, MIXED_ID = _id("7"), _id("6"), _id("5")
PG_ID, REDIS_ID, HAPROXY_ID, BARE_ID = _id("4"), _id("3"), _id("2"), _id("1")

MCP_PROFILES = (
    "at-mcp-analysis-readonly",
    "at-mcp-account-read",
    "at-mcp-tradingcodex-execution",
    "at-mcp-paper-001",
    "at-mcp-kiwoom",
    "at-mcp-live-kr",
    "at-mcp-live-us",
    "at-mcp-live-crypto",
    "at-mcp-h3-crypto-paper",
    "at-mcp-h3-us-paper",
)
UNITS = (
    "at-api-blue",
    "at-worker",
    "at-scheduler",
    "at-upbit-ws",
    "at-mcp-blue",
    *MCP_PROFILES,
)
PRUNABLE = {STALE_ID, DANGLING_ID, MULTI_ID, NEAR_ID}
# Without --skip-kis-ws the KIS unit is replaced, so its old image is unused.
PRUNED_BY_DEPLOY = PRUNABLE | {KIS_ID}
FOREIGN = {DEV_ID, BACKUP_ID, MIXED_ID, PG_ID, REDIS_ID, HAPROXY_ID, BARE_ID}

FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env -S python3 -S -E
    import json, os, pathlib, sys
    args = sys.argv[1:]
    state_file = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
    state = json.loads(state_file.read_text())
    with pathlib.Path(os.environ['FAKE_DOCKER_LOG']).open('a') as f:
        f.write(json.dumps(args) + '\\n')
    faults = set(filter(None, os.environ.get('FAKE_FAULTS', '').split(',')))
    images, containers = state['images'], state['containers']

    def save():
        state_file.write_text(json.dumps(state))

    def fail(message, code=1):
        print(message, file=sys.stderr)
        sys.exit(code)

    def resolve(ref):
        if ref in images:
            return ref
        for image_id, image in images.items():
            if ref in image['tags'] or ref in image['digests']:
                return image_id
        return None

    def repository(ref):
        ref = ref.split('@', 1)[0]
        head, sep, tail = ref.rpartition(':')
        return head if sep and '/' not in tail else ref

    def container_ids():
        return {'cid-' + name: name for name in containers}

    def in_use(image_id):
        return any(c['image'] == image_id for c in containers.values())

    cmd = args[0]
    if cmd == 'inspect':
        fmt, name = args[2], args[-1]
        name = container_ids().get(name, name)
        if name not in containers:
            fail('Error: No such object: ' + name)
        c = containers[name]
        if 'State.Running' in fmt:
            print('true' if c['running'] else 'false')
        elif 'Config.Image' in fmt:
            print(c['config'])
        elif 'RepoDigests' in fmt:
            fail('template: executing "" at <index .RepoDigests 0>: error calling index')
        elif fmt == '{{.Image}}':
            if 'container_inspect' in faults:
                fail('Cannot connect to the Docker daemon')
            print(c['image'])
        else:
            print('cid-' + name)
    elif cmd == 'image' and args[1] == 'inspect':
        fmt, ref = args[3], args[4]
        image_id = resolve(ref)
        if image_id is None:
            fail('Error: No such image: ' + ref)
        image = images[image_id]
        if fmt == '{{index .RepoDigests 0}}':
            print(image['digests'][0] if image['digests'] else '')
        elif fmt == '{{.Id}}':
            print(image_id)
        elif fmt == '{{.Size}}':
            print(image['size'] if 'no_size' not in faults else '<no value>')
        elif fmt.startswith('{{range .RepoTags}}'):
            if 'refs_inspect' in faults:
                fail('Cannot connect to the Docker daemon')
            sys.stdout.write(''.join(r + '\\n' for r in image['tags'] + image['digests']))
        else:
            fail('unsupported image inspect format: ' + fmt, 42)
    elif cmd == 'image' and args[1] == 'ls':
        if 'image_ls' in faults:
            fail('Cannot connect to the Docker daemon')
        for image_id, image in images.items():
            for _ in image['tags'] or ['<none>']:
                print(image_id)
    elif cmd == 'image' and args[1] == 'rm':
        failed = False
        for ref in args[2:]:
            if ref.startswith('-'):
                fail('unexpected image rm flag: ' + ref, 42)
            image_id = resolve(ref)
            if image_id is None or ('rm:' + ref) in faults:
                print('Error response from daemon: cannot remove ' + ref, file=sys.stderr)
                failed = True
                continue
            image = images[image_id]
            refs = image['tags'] + image['digests']
            if ('noop:' + ref) in faults:
                print('Untagged: ' + ref)
                continue
            if ref == image_id or (len(refs) == 1 and in_use(image_id)):
                print('Error response from daemon: conflict: unable to remove ' + ref
                      + ' (must force) - image is being used by a container', file=sys.stderr)
                failed = True
                continue
            if ref in image['tags']:
                image['tags'].remove(ref)
                # Daemon behavior: once a repository has no tag left on the
                # image, its digest references of that repository go too.
                repo = repository(ref)
                if not any(repository(t) == repo for t in image['tags']):
                    image['digests'] = [d for d in image['digests'] if repository(d) != repo]
            else:
                image['digests'].remove(ref)
            print('Untagged: ' + ref)
            if not image['tags'] and not image['digests']:
                del images[image_id]
                print('Deleted: ' + image_id)
        save()
        sys.exit(1 if failed else 0)
    elif cmd == 'ps':
        if 'ps' in faults and '--no-trunc' in args:
            fail('Cannot connect to the Docker daemon')
        for name, c in containers.items():
            if c['running'] or '-a' in args:
                print(('cid-' + name) if '{{.ID}}' in args else name)
    elif cmd == 'pull':
        ref = args[1]
        pulled = state['registry'].get(ref, ref)
        image_id = resolve(pulled)
        if image_id is None:
            remote = {i: im for i, im in state['registry_images'].items() if pulled in im['digests']}
            if not remote:
                fail('manifest unknown: ' + ref)
            image_id = next(iter(remote))
            images[image_id] = remote[image_id]
        if ':' in ref.split('/')[-1] and '@' not in ref:
            for image in images.values():
                if ref in image['tags']:
                    image['tags'].remove(ref)
            images[image_id]['tags'].append(ref)
    elif cmd == 'run':
        name = args[args.index('--name') + 1]
        ref = next((a for a in args if a.startswith('ghcr.io/') or a.startswith('haproxy:')), None)
        image_id = resolve(ref) if ref else None
        if image_id is None:
            fail('Unable to find image ' + str(ref), 125)
        if name == os.environ.get('FAKE_FAIL_RUN_NAME') and image_id == os.environ['FAKE_NEW_ID']:
            sys.exit(23)
        containers[name] = {'image': image_id, 'config': ref, 'running': True}
        print('cid-' + name)
    elif cmd == 'rm':
        for item in args[1:]:
            if item != '-f':
                containers.pop(item, None)
    elif cmd == 'rename':
        containers[args[2]] = containers.pop(args[1])
    elif cmd == 'logs':
        print('Listening started connected=True')
    elif cmd in {'stop', 'kill'}:
        pass
    else:
        fail('fake docker refuses: ' + ' '.join(args), 42)
    save()
    """
)


def _image(tags: list[str], digests: list[str], size: int) -> dict:
    return {"tags": tags, "digests": digests, "size": size}


def _initial_state(*, skipped_kis_stopped: bool) -> dict:
    images = {
        OLD_ID: _image([], [OLD], 1000),
        # Registry `:main` has already moved to NEW; the local store does not
        # have NEW until the deploy pulls it.
        KIS_ID: _image([f"{REPOSITORY}:sha-kis"], [KIS], 1100),
        PREVX_ID: _image([f"{REPOSITORY}:sha-prevx"], [PREVX], 1200),
        STALE_ID: _image([f"{REPOSITORY}:sha-stale"], [STALE], 100),
        DANGLING_ID: _image([], [DANGLING], 200),
        MULTI_ID: _image(
            [f"{REPOSITORY}:sha-m1", f"{REPOSITORY}:sha-m2"], [MULTI], 300
        ),
        STOPPED_ID: _image([f"{REPOSITORY}:sha-stopped"], [STOPPED], 1300),
        NEAR_ID: _image([], [NEAR], 400),
        DEV_ID: _image(
            [f"{REPOSITORY}-dev:main"], [f"{REPOSITORY}-dev@sha256:" + "a" * 64], 50
        ),
        BACKUP_ID: _image([f"{REPOSITORY}_backup:old"], [], 50),
        MIXED_ID: _image(
            [f"{REPOSITORY}:sha-mixed", "mirror.example.com/auto_trader:sha-mixed"],
            [],
            50,
        ),
        PG_ID: _image(["postgres:16"], ["postgres@sha256:" + "b" * 64], 50),
        REDIS_ID: _image(["redis:7"], ["redis@sha256:" + "c" * 64], 50),
        HAPROXY_ID: _image(["haproxy:3.1-alpine"], ["haproxy@sha256:" + "d" * 64], 50),
        BARE_ID: _image([], [], 50),
    }
    containers = {
        name: {"image": OLD_ID, "config": OLD, "running": True} for name in UNITS
    }
    containers["at-kis-ws"] = {
        "image": KIS_ID,
        "config": KIS,
        "running": not skipped_kis_stopped,
    }
    containers["at-haproxy"] = {
        "image": HAPROXY_ID,
        "config": "haproxy:3.1-alpine",
        "running": True,
    }
    containers["at-postgres"] = {
        "image": PG_ID,
        "config": "postgres:16",
        "running": True,
    }
    # A stopped one-off container of our repository: its image is in use.
    containers["at-oneoff"] = {"image": STOPPED_ID, "config": STOPPED, "running": False}
    return {
        "images": images,
        "containers": containers,
        # NEW exists only in the registry until the deploy pulls `:main`.
        "registry": {f"{REPOSITORY}:main": NEW},
        "registry_images": {NEW_ID: _image([], [NEW], 2000)},
    }


class Env:
    def __init__(self, tmp_path: Path, *, skipped_kis_stopped: bool = False) -> None:
        tmp_path.mkdir(exist_ok=True)
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        self.log = tmp_path / "docker-calls.jsonl"
        self.state_path = tmp_path / "state.json"
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        state = _initial_state(skipped_kis_stopped=skipped_kis_stopped)
        self.state_path.write_text(json.dumps(state))
        (self.bindir / "docker").write_text(FAKE_DOCKER)
        (self.bindir / "curl").write_text(
            "#!/usr/bin/env bash\n"
            'url="${!#}"\n'
            'if [[ "${FAKE_FAIL_HEALTH:-}" == 1 && "$url" == *":8002/healthz" ]]; then echo 500; exit 0; fi\n'
            'if [[ "$*" == *--write-out* ]]; then echo 200; fi\n'
        )
        (self.bindir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
        (self.bindir / "nohup").write_text("#!/usr/bin/env bash\nexit 0\n")
        for path in self.bindir.iterdir():
            path.chmod(0o755)
        (self.run_dir / ".env.runtime").write_text("x=y\n")
        (self.run_dir / ".env.secrets").write_text(
            "".join(
                f"{name}=x\n"
                for name in (
                    "MCP_AUTH_TOKEN",
                    "MCP_ANALYSIS_READONLY_AUTH_TOKEN",
                    "MCP_ACCOUNT_READ_AUTH_TOKEN",
                    "MCP_TRADINGCODEX_EXECUTION_AUTH_TOKEN",
                    "MCP_PAPER_001_AUTH_TOKEN",
                    "MCP_KIWOOM_AUTH_TOKEN",
                    "MCP_LIVE_KR_AUTH_TOKEN",
                    "MCP_LIVE_US_AUTH_TOKEN",
                    "MCP_LIVE_CRYPTO_AUTH_TOKEN",
                    "MCP_H3_CRYPTO_PAPER_AUTH_TOKEN",
                    "MCP_H3_US_PAPER_AUTH_TOKEN",
                )
            )
        )
        # The recorded current deployment is PREVX (no container runs it), so
        # after a successful deploy PREVX is the rollback target that only the
        # KEEP-2 rule protects.
        (self.run_dir / "deployed-digest").write_text(PREVX + "\n")
        (self.run_dir / "deployed-digest.previous").write_text(STALE + "\n")
        (self.run_dir / "api-active-color").write_text("blue\n")
        (self.run_dir / "mcp-active-color").write_text("blue\n")

    def put_new_locally_as_main(self) -> None:
        """Dry-run inspects `:main` locally; model an earlier pull of it."""
        state = self.state()
        state["images"][NEW_ID] = _image([f"{REPOSITORY}:main"], [NEW], 2000)
        self.state_path.write_text(json.dumps(state))

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def run(
        self,
        *args: str,
        deploy: Path | None = None,
        faults: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        fail_health: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        # The switch is unset unless a test sets it: unset is the default-on
        # case, and an inherited value must not leak into the fixture.
        base = {k: v for k, v in os.environ.items() if k != "AT_IMAGE_PRUNE_ENABLED"}
        return subprocess.run(
            [str(deploy or DEPLOY), *args],
            capture_output=True,
            text=True,
            timeout=90,
            env={
                **base,
                "PATH": f"{self.bindir}:{os.environ['PATH']}",
                "AT_RUN_DIRECTORY": str(self.run_dir),
                "MCP_HAPROXY_TEMPLATE": str(REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"),
                "FAKE_DOCKER_STATE": str(self.state_path),
                "FAKE_DOCKER_LOG": str(self.log),
                "FAKE_FAULTS": ",".join(faults),
                "FAKE_NEW_ID": NEW_ID,
                "FAKE_FAIL_HEALTH": "1" if fail_health else "0",
                "AT_HEALTHZ_ATTEMPTS": "1",
                "AT_HEALTHZ_SLEEP_SECONDS": "0",
                "MCP_HEALTH_ATTEMPTS": "1",
                "MCP_HEALTH_SLEEP_SECONDS": "0",
                "HAPROXY_READY_ATTEMPTS": "1",
                "HAPROXY_READY_INTERVAL": "0",
                **(env or {}),
            },
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _rm_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["image", "rm"]]


def _refs_of(state: dict, image_id: str) -> set[str]:
    image = state["images"][image_id]
    return set(image["tags"]) | set(image["digests"])


def _keep_violations(
    before: dict, after: dict, calls: list[list[str]], kept: set[str]
) -> list[str]:
    """Every invariant the prune must hold, as sentences naming the image."""
    violations = []
    attempted = {ref for call in _rm_calls(calls) for ref in call[2:]}
    for image_id in kept:
        if image_id not in after["images"]:
            violations.append(f"kept image {image_id} was removed")
        refs = _refs_of(before, image_id) if image_id in before["images"] else set()
        if refs & attempted:
            violations.append(f"removal was attempted on kept image {image_id}")
    for image_id in FOREIGN:
        if image_id not in after["images"]:
            violations.append(f"foreign image {image_id} was removed")
        if _refs_of(before, image_id) & attempted:
            violations.append(f"removal was attempted on foreign image {image_id}")
    for call in calls:
        if call[0] in {"system", "volume", "network", "builder", "container"}:
            violations.append(f"forbidden docker call {call}")
        if "prune" in call:
            violations.append(f"docker prune call {call}")
        if call[:2] == ["image", "rm"] and any(a.startswith("-") for a in call[2:]):
            violations.append(f"forced image removal {call}")
    return violations


def _deploy_keep_set(*, previous: str, skip_kis: bool = False) -> set[str]:
    # OLD stays in use by the draining previous API and MCP colors.
    in_use = {NEW_ID, OLD_ID, STOPPED_ID} | ({KIS_ID} if skip_kis else set())
    return in_use | {previous}


def test_successful_deploy_prunes_only_unused_images_of_the_repository(
    env: Env,
) -> None:
    before = env.state()
    result = env.run()
    after = env.state()
    calls = env.calls()
    assert result.returncode == 0, result.stderr
    assert "deployment completed: " + NEW in result.stdout
    assert "MISMATCH" not in result.stdout
    assert (env.run_dir / "deployed-digest.previous").read_text() == PREVX + "\n"
    assert (
        _keep_violations(before, after, calls, _deploy_keep_set(previous=PREVX_ID))
        == []
    )
    for image_id in PRUNED_BY_DEPLOY:
        assert image_id not in after["images"], image_id
        assert f"image prune: removed {image_id}" in result.stdout
    assert "image prune: removed 5 image(s), reclaimed 2100 bytes" in result.stdout
    assert "WARNING" not in result.stderr


def test_prune_runs_after_promotion_and_digest_record(env: Env) -> None:
    result = env.run()
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.index("deployment completed:") < out.index("image prune: removed")
    calls = env.calls()
    first_ls = calls.index(["image", "ls", "--no-trunc", "--format", "{{.ID}}"])
    last_run = max(i for i, c in enumerate(calls) if c[0] == "run")
    assert last_run < first_ls


def test_multi_tag_image_is_removed_with_every_reference_in_one_call(
    env: Env,
) -> None:
    result = env.run()
    assert result.returncode == 0, result.stderr
    multi_calls = [c for c in _rm_calls(env.calls()) if MULTI in c]
    assert len(multi_calls) == 1
    assert set(multi_calls[0][2:]) == {
        f"{REPOSITORY}:sha-m1",
        f"{REPOSITORY}:sha-m2",
        MULTI,
    }
    # The daemon drops MULTI's digest with its last tag, so the digest argument
    # errors; the image is gone and the outcome is still a clean removal.
    assert MULTI_ID not in env.state()["images"]
    assert f"image prune: removed {MULTI_ID}" in result.stdout
    assert f"could not remove {MULTI_ID}" not in result.stderr


def test_skipped_and_stopped_kis_image_is_kept(tmp_path: Path) -> None:
    env = Env(tmp_path, skipped_kis_stopped=True)
    before = env.state()
    result = env.run("--skip-kis-ws")
    after = env.state()
    assert result.returncode == 0, result.stderr
    assert "SKIPPED_STOPPED" in result.stdout
    assert after["containers"]["at-kis-ws"]["image"] == KIS_ID
    assert (
        _keep_violations(
            before,
            after,
            env.calls(),
            _deploy_keep_set(previous=PREVX_ID, skip_kis=True),
        )
        == []
    )


def test_running_skipped_kis_image_is_kept(env: Env) -> None:
    before = env.state()
    result = env.run("--skip-kis-ws")
    assert result.returncode == 0, result.stderr
    after = env.state()
    assert after["containers"]["at-kis-ws"]["image"] == KIS_ID
    assert (
        _keep_violations(
            before,
            after,
            env.calls(),
            _deploy_keep_set(previous=PREVX_ID, skip_kis=True),
        )
        == []
    )
    assert "image prune: removed 4 image(s), reclaimed 1000 bytes" in result.stdout


def test_failed_deploy_never_prunes(env: Env) -> None:
    before = env.state()
    result = env.run(fail_health=True)
    assert result.returncode != 0
    calls = env.calls()
    assert not any(c[:2] in (["image", "ls"], ["image", "rm"]) for c in calls)
    assert "image prune" not in result.stdout + result.stderr
    assert set(env.state()["images"]) == set(before["images"]) | {NEW_ID}


def test_manual_rollback_never_prunes(env: Env) -> None:
    (env.run_dir / "deployed-digest.previous").write_text(PREVX + "\n")
    before = env.state()
    result = env.run("--rollback")
    assert result.returncode == 0, result.stderr
    assert "deployment completed: " + PREVX in result.stdout
    calls = env.calls()
    assert not any(c[:2] in (["image", "ls"], ["image", "rm"]) for c in calls)
    assert "image prune" not in result.stdout + result.stderr
    assert set(env.state()["images"]) == set(before["images"])


def test_rollback_after_prune_uses_the_locally_kept_previous_digest(
    env: Env,
) -> None:
    first = env.run()
    assert first.returncode == 0, first.stderr
    after_prune = env.state()
    assert PREVX_ID in after_prune["images"]
    assert PREVX in _refs_of(after_prune, PREVX_ID)
    assert (env.run_dir / "deployed-digest.previous").read_text() == PREVX + "\n"
    # Rollback must not need the registry: drop every registry entry.
    state = env.state()
    state["registry"], state["registry_images"] = {}, {}
    env.state_path.write_text(json.dumps(state))

    second = env.run("--rollback")
    assert second.returncode == 0, second.stderr
    assert "deployment completed: " + PREVX in second.stdout
    rolled = env.state()
    for name in ("at-worker", "at-scheduler", "at-upbit-ws", *MCP_PROFILES):
        assert rolled["containers"][name]["image"] == PREVX_ID, name
        assert rolled["containers"][name]["config"] == PREVX, name
    assert ["pull", PREVX] in env.calls()
    assert _rm_calls(env.calls()) == []


def test_dry_run_lists_would_be_removals_and_removes_nothing(env: Env) -> None:
    env.put_new_locally_as_main()
    before = env.state()
    result = env.run("--dry-run")
    assert result.returncode == 0, result.stderr
    calls = env.calls()
    assert _rm_calls(calls) == []
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "rename", "kill"} for c in calls
    )
    assert set(env.state()["images"]) == set(before["images"])
    for image_id in PRUNABLE:
        assert f"image prune: would remove {image_id}" in result.stdout
    for image_id in {NEW_ID, PREVX_ID, OLD_ID, KIS_ID, STOPPED_ID, *FOREIGN}:
        assert f"would remove {image_id}" not in result.stdout, image_id
    assert "image prune: would remove 4 image(s)" in result.stdout


def test_dry_run_with_nul_previous_record_says_prune_would_be_skipped(
    env: Env,
) -> None:
    env.put_new_locally_as_main()
    (env.run_dir / "deployed-digest").write_text("invalid\n")
    (env.run_dir / "deployed-digest.previous").write_bytes((PREVX + "\n\x00").encode())
    result = env.run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert "image prune: would be skipped" in result.stdout
    assert "would remove sha256:" not in result.stdout
    assert _rm_calls(env.calls()) == []


def test_rollback_dry_run_states_prune_is_not_run(env: Env) -> None:
    (env.run_dir / "deployed-digest.previous").write_text(PREVX + "\n")
    result = env.run("--rollback", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "image prune: not run on rollback" in result.stdout
    assert not any(c[:2] in (["image", "ls"], ["image", "rm"]) for c in env.calls())


@pytest.mark.parametrize(
    "fault",
    (
        "rm:" + f"{REPOSITORY}:sha-stale",
        "noop:" + f"{REPOSITORY}:sha-stale",
        "image_ls",
        "ps",
        "container_inspect",
        "refs_inspect",
    ),
)
def test_prune_errors_are_warnings_and_never_change_the_deploy_result(
    env: Env, fault: str
) -> None:
    before = env.state()
    result = env.run(faults=(fault,))
    after = env.state()
    assert result.returncode == 0, result.stderr
    assert "deployment completed: " + NEW in result.stdout
    assert "MISMATCH" not in result.stdout
    assert "WARNING: image prune" in result.stderr
    assert (
        _keep_violations(
            before, after, env.calls(), _deploy_keep_set(previous=PREVX_ID)
        )
        == []
    )
    if fault.startswith(("rm:", "noop:")):
        assert STALE_ID in after["images"]
        assert f"could not remove {STALE_ID}" in result.stderr
        assert f"{REPOSITORY}:sha-stale" in result.stderr
        # Other candidates are still removed.
        assert DANGLING_ID not in after["images"]
        assert "image prune: removed 4 image(s)" in result.stdout
        if fault.startswith("noop:"):
            assert "still present" in result.stderr
    else:
        assert _rm_calls(env.calls()) == []
        assert set(after["images"]) == set(before["images"]) | {NEW_ID}


def test_size_unavailable_reports_count(env: Env) -> None:
    result = env.run(faults=("no_size",))
    assert result.returncode == 0, result.stderr
    assert "image prune: removed 5 image(s), reclaimed 0 bytes" in result.stdout
    assert "size unavailable for 5 image(s)" in result.stdout


@pytest.mark.parametrize(
    "content",
    (
        "",
        "garbage\n",
        f"{REPOSITORY}:sha-prevx\n",
        None,
        # A valid first line does not make the record valid (tester r1).
        PREVX + "\nINVALID-SECOND-LINE\n",
        PREVX + "\n" + STALE + "\n",
        # Bash drops NUL bytes from read and command substitution (tester r2).
        PREVX + "\n\x00",
        PREVX + "\x00\n",
        "\x00" + PREVX + "\n",
        PREVX + "\r\n",
        PREVX,  # write_digest always terminates the record
        PREVX + "\n\n",
        " " + PREVX + "\n",
    ),
)
def test_invalid_previous_record_skips_prune_entirely(
    env: Env, content: str | None
) -> None:
    # An invalid current record is not rotated by write_digest, so the
    # previous record stays exactly as written here.
    (env.run_dir / "deployed-digest").write_text("invalid\n")
    previous = env.run_dir / "deployed-digest.previous"
    if content is None:
        previous.unlink()
    else:
        previous.write_bytes(content.encode())
    before = env.state()
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "deployment completed: " + NEW in result.stdout
    assert "WARNING: image prune skipped" in result.stderr
    assert "deployed-digest.previous is absent or invalid" in result.stderr
    calls = env.calls()
    assert not any(c[:2] in (["image", "ls"], ["image", "rm"]) for c in calls)
    assert set(env.state()["images"]) == set(before["images"]) | {NEW_ID}


@pytest.mark.parametrize("value", ("0", "no", "true", "", " 1"))
def test_env_switch_disables_prune(env: Env, value: str) -> None:
    before = env.state()
    result = env.run(env={"AT_IMAGE_PRUNE_ENABLED": value})
    assert result.returncode == 0, result.stderr
    assert not any(c[:2] in (["image", "ls"], ["image", "rm"]) for c in env.calls())
    assert set(env.state()["images"]) == set(before["images"]) | {NEW_ID}
    if value == "0":
        assert "image prune: disabled by AT_IMAGE_PRUNE_ENABLED=0" in result.stdout
    else:
        assert "AT_IMAGE_PRUNE_ENABLED must be 0 or 1" in result.stderr


def test_env_switch_default_is_on(env: Env) -> None:
    result = env.run()  # AT_IMAGE_PRUNE_ENABLED unset
    assert result.returncode == 0, result.stderr
    assert "image prune: removed 5 image(s)" in result.stdout


def test_skipped_mcp_unit_with_its_own_digest_is_kept(env: Env) -> None:
    # Tester r1 SHOULD: a skipped MCP unit on a digest no other unit uses.
    state = env.state()
    state["containers"]["at-mcp-kiwoom"] = {
        "image": STALE_ID,
        "config": STALE,
        "running": True,
    }
    env.state_path.write_text(json.dumps(state))
    before = env.state()
    result = env.run(env={"MCP_UNITS_SKIP": "kiwoom"})
    after = env.state()
    assert result.returncode == 0, result.stderr
    assert f"at-mcp-kiwoom\t{STALE}\t{STALE}\tMATCH" in result.stdout
    assert after["containers"]["at-mcp-kiwoom"]["image"] == STALE_ID
    assert (
        _keep_violations(
            before,
            after,
            env.calls(),
            _deploy_keep_set(previous=PREVX_ID) | {STALE_ID},
        )
        == []
    )


# --- mutants: one assertion-RED mutant per KEEP rule in the contract --------

KEEP_MUTANTS = {
    "KEEP-1": (
        r'  \[\[ -n "\$current" \]\] && keep_refs\+=\("\$current"\) # KEEP-1 current\n',
        "",
    ),
    "KEEP-2": (r'  keep_refs\+=\("\$previous"\) # KEEP-2 previous\n', ""),
    "KEEP-3": (
        r'    keep_ids\["\$image_id"\]="used by a container" # KEEP-3 in-use\n',
        "",
    ),
    "KEEP-4": (
        r'== "\$IMAGE_REPOSITORY" \]\]; then owned=true',
        '== "$IMAGE_REPOSITORY"* ]]; then owned=true',
    ),
}
EXTRA_MUTANTS = {
    "stopped containers ignored": (r"docker ps -a --no-trunc", "docker ps --no-trunc"),
    "mixed-repository image treated as ours": (
        r'\[\[ "\$owned" == true && "\$foreign" == false \]\]',
        '[[ "$owned" == true ]]',
    ),
    "prune on rollback": (
        r'manual_rollback\(\) \{(.*?)promote_digest "\$previous"; \}',
        r'manual_rollback() {\1promote_digest "$previous"; run_image_prune "$previous"; }',
    ),
    "previous record read by first line only": (
        r'previous="\$\(read_digest_record "\$DEPLOYED_DIGEST_PREVIOUS_FILE"\)" \|\| \{ printf \'WARNING',
        'previous="$(read_digest "$DEPLOYED_DIGEST_PREVIOUS_FILE")" || { printf \'WARNING',
    ),
    "record size unchecked": (
        r'  \[\[ "\$size" == "\$\(\(\$\{#line\} \+ 1\)\)" \]\] \|\| return 1 # record size\n',
        "",
    ),
    "empty switch treated as on": (
        r'"\$\{AT_IMAGE_PRUNE_ENABLED-1\}"',
        '"${AT_IMAGE_PRUNE_ENABLED:-1}"',
    ),
    "prune error changes rc": (
        r"run_image_prune\(\) \{ \(prune_old_images \"\$1\"\) \|\| printf '[^']*' >&2; \}",
        'run_image_prune() { prune_old_images "$1"; }',
    ),
}


def test_contract_keep_rule_count_matches_mutants_on_disk() -> None:
    rules = re.findall(r"^- (KEEP-\d+) ", CONTRACT.read_text(), flags=re.MULTILINE)
    assert rules == sorted(set(rules))
    assert set(rules) == set(KEEP_MUTANTS)
    script = DEPLOY.read_text()
    for rule in rules:
        assert f"# {rule} " in script, rule


def _mutant(tmp_path: Path, pattern: str, replacement: str) -> Path:
    source = DEPLOY.read_text()
    mutated, count = re.subn(pattern, replacement, source, count=1, flags=re.DOTALL)
    assert count == 1, pattern
    path = tmp_path / "deploy-mutant.sh"
    path.write_text(mutated)
    path.chmod(0o755)
    return path


def _dry_run_violations(env: Env, deploy: Path | None) -> list[str]:
    env.put_new_locally_as_main()
    result = env.run("--dry-run", deploy=deploy)
    assert result.returncode == 0, result.stderr
    return [
        f"dry-run would remove kept image {image_id}"
        for image_id in (NEW_ID, PREVX_ID)
        if f"would remove {image_id}" in result.stdout
    ]


def _deploy_violations(env: Env, deploy: Path | None, *args: str) -> list[str]:
    before = env.state()
    result = env.run(*args, deploy=deploy)
    assert result.returncode == 0, result.stderr
    return _keep_violations(
        before, env.state(), env.calls(), _deploy_keep_set(previous=PREVX_ID)
    )


@pytest.mark.parametrize("rule", sorted(KEEP_MUTANTS))
def test_keep_rule_mutant_is_red(tmp_path: Path, rule: str) -> None:
    mutant = _mutant(tmp_path, *KEEP_MUTANTS[rule])
    clean = Env(tmp_path / "clean")
    mutated = Env(tmp_path / "mutated")
    if rule == "KEEP-1":
        # After a real deploy every unit runs NEW, so KEEP-3 also covers it;
        # the dry-run plan exercises KEEP-1 alone through the same planner.
        assert _dry_run_violations(clean, None) == []
        assert _dry_run_violations(mutated, mutant) == [
            f"dry-run would remove kept image {NEW_ID}"
        ]
        return
    assert _deploy_violations(clean, None) == []
    violations = _deploy_violations(mutated, mutant)
    expected = {
        "KEEP-2": f"kept image {PREVX_ID} was removed",
        "KEEP-3": f"removal was attempted on kept image {OLD_ID}",
        "KEEP-4": f"foreign image {DEV_ID} was removed",
    }[rule]
    assert expected in violations


@pytest.mark.parametrize("name", sorted(EXTRA_MUTANTS))
def test_extra_mutant_is_red(tmp_path: Path, name: str) -> None:
    mutant = _mutant(tmp_path, *EXTRA_MUTANTS[name])
    env = Env(tmp_path / "e")
    if name == "prune on rollback":
        (env.run_dir / "deployed-digest.previous").write_text(PREVX + "\n")
        result = env.run("--rollback", deploy=mutant)
        assert result.returncode == 0, result.stderr
        assert any(c[:2] == ["image", "ls"] for c in env.calls())
        return
    if name in {
        "previous record read by first line only",
        "record size unchecked",
        "empty switch treated as on",
    }:
        if name != "empty switch treated as on":
            (env.run_dir / "deployed-digest").write_text("invalid\n")
            record = PREVX + "\nINVALID-SECOND-LINE\n"
            if name == "record size unchecked":
                record = PREVX + "\n\x00"
            (env.run_dir / "deployed-digest.previous").write_bytes(record.encode())
            result = env.run(deploy=mutant)
        else:
            result = env.run(env={"AT_IMAGE_PRUNE_ENABLED": ""}, deploy=mutant)
        assert result.returncode == 0, result.stderr
        # Both inputs are invalid, so any removal attempt is the violation.
        assert _rm_calls(env.calls()) != []
        return
    if name == "prune error changes rc":
        result = env.run(faults=("rm:" + f"{REPOSITORY}:sha-stale",), deploy=mutant)
        assert result.returncode != 0
        return
    violations = _deploy_violations(env, mutant)
    expected = {
        "stopped containers ignored": f"removal was attempted on kept image {STOPPED_ID}",
        "mixed-repository image treated as ours": f"removal was attempted on foreign image {MIXED_ID}",
    }[name]
    assert expected in violations
