"""hk 3578: image builds cache to GHCR `:buildcache`, never to the Actions cache.

The Actions cache is 10GB per repository and evicts least-recently-used entries when it
fills. `type=gha` docker caches filled it with `buildkit-blob-*` entries (9.78GB of 12.0GB
on 2026-11-08), which evicted the pinned toolchain caches of hk 3559. So:

1. Only `deploy.yml` and `build-caddy.yml` write a docker build cache. Each writes to a
   `type=registry` ref under its own image repository, tag `buildcache`, after a GHCR login.
2. No `cache-to` or `cache-from` under `.github/` uses `type=gha`, in any spelling.
3. Nothing under `.github/` reads a docker cache it cannot authenticate for: the any-ref
   build in `deploy-checks.yml` has no GHCR login, so it has no cache at all.
4. The `buildcache` tag is never a tag a host pulls or that the deploy scripts resolve.

The tests read the workflow YAML, so they pin behaviour rather than spelling, and the
text scan catches a `type=gha` in any file, including ones this test does not parse.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"
SCRIPTS = REPO_ROOT / "scripts"

GHA_CACHE_RE = re.compile(r"type\s*=\s*gha\b")
REGISTRY_BUILDCACHE_RE = re.compile(
    r"^type=registry,ref=\$\{\{ env\.REGISTRY \}\}/\$\{\{ env\.IMAGE_NAME \}\}"
    r"(\$\{\{ matrix\.suffix \}\})?:buildcache,mode=max"
    r"(,image-manifest=true,oci-mediatypes=true)?$"
)
REGISTRY_READ_RE = re.compile(
    r"^type=registry,ref=\$\{\{ env\.REGISTRY \}\}/\$\{\{ env\.IMAGE_NAME \}\}"
    r"(\$\{\{ matrix\.suffix \}\})?:buildcache$"
)
CACHE_WRITING_WORKFLOWS = {"deploy.yml", "build-caddy.yml"}


def _github_yaml_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *GITHUB.rglob("action.yml")])


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _cache_entries(doc: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(job, step name, key, value) for every cache-from / cache-to input in a workflow."""
    out: list[tuple[str, str, str, dict[str, Any]]] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            with_block = step.get("with") or {}
            for key in ("cache-from", "cache-to"):
                if key in with_block:
                    out.append((job_name, step.get("name", ""), key, step))
    return out


def test_no_github_file_uses_the_actions_cache_for_docker() -> None:
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _github_yaml_files()
        if GHA_CACHE_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_only_deploy_and_caddy_write_a_docker_build_cache() -> None:
    writers = {
        path.name
        for path in _github_yaml_files()
        for _job, _name, key, _step in _cache_entries(_load(path))
        if key == "cache-to"
    }
    assert writers == CACHE_WRITING_WORKFLOWS


def test_every_cache_to_is_a_registry_buildcache_ref() -> None:
    values = [
        (path.name, job, step["with"]["cache-to"])
        for path in _github_yaml_files()
        for job, _name, key, step in _cache_entries(_load(path))
        if key == "cache-to"
    ]
    assert len(values) == 2, values
    for file_name, job, value in values:
        assert REGISTRY_BUILDCACHE_RE.match(str(value).strip()), (file_name, job, value)


def test_every_cache_from_is_a_registry_buildcache_ref() -> None:
    for path in _github_yaml_files():
        for job, _name, key, step in _cache_entries(_load(path)):
            if key != "cache-from":
                continue
            value = str(step["with"]["cache-from"]).strip()
            assert REGISTRY_READ_RE.match(value), (path.name, job, value)


def test_every_cache_write_follows_a_ghcr_login_in_the_same_job() -> None:
    for name in CACHE_WRITING_WORKFLOWS:
        doc = _load(WORKFLOWS / name)
        for job_name, job in doc["jobs"].items():
            steps = job.get("steps") or []
            writes = [
                i for i, s in enumerate(steps) if "cache-to" in (s.get("with") or {})
            ]
            if not writes:
                continue
            logins = [
                i
                for i, s in enumerate(steps)
                if "docker/login-action" in s.get("uses", "")
                and (s.get("with") or {}).get("registry") == "${{ env.REGISTRY }}"
            ]
            assert logins and logins[0] < writes[0], (name, job_name)
            assert job["permissions"]["packages"] == "write", (name, job_name)


def test_deploy_checks_has_no_docker_cache_because_it_cannot_authenticate() -> None:
    # The any-ref build runs without a GHCR login (see the login test in
    # test_deploy_tier_workflow_contract.py), so an unauthenticated cache read
    # is not allowed here. Reading it would need that contract changed first.
    doc = _load(WORKFLOWS / "deploy-checks.yml")
    entries = [(job, key) for job, _name, key, _step in _cache_entries(doc)]
    assert entries == [], entries


def test_buildcache_tag_is_never_one_a_host_pulls_or_the_deploy_resolves() -> None:
    for script in (
        SCRIPTS / "deploy-ncp-pull.sh",
        SCRIPTS / "deploy_ci_gate.py",
    ):
        assert "buildcache" not in script.read_text(encoding="utf-8"), script.name
