from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_local_compose_builds_pass_vcs_ref_to_dockerfile_api() -> None:
    expected_build_block = (
        "build:\n"
        "      context: .\n"
        "      dockerfile: Dockerfile.api\n"
        "      args:\n"
        "        VCS_REF: ${VCS_REF:-}"
    )

    for compose_path in ("docker-compose.api.yml", "docker-compose.full.yml"):
        compose_text = _read(compose_path)

        assert compose_text.count(expected_build_block) == 3


def test_local_docker_entrypoints_compute_git_sha_for_builds() -> None:
    compose_script = _read("run_api_compose.sh")
    docker_script = _read("run_docker.sh")
    makefile = _read("Makefile")

    assert 'vcs_ref="$(git rev-parse HEAD)"' in compose_script
    assert (
        'VCS_REF="$vcs_ref" docker compose -f docker-compose.api.yml up -d --build'
        in compose_script
    )

    assert 'vcs_ref="$(git rev-parse HEAD)"' in docker_script
    expected_direct_build_command = (
        'docker build --build-arg VCS_REF="$vcs_ref" -f Dockerfile.api '
        + "-t auto_trader-api:local ."
    )
    assert expected_direct_build_command in docker_script

    assert "docker-build: ## Build Docker image" in makefile
    assert 'vcs_ref="$$(git rev-parse HEAD)"; \\' in makefile
    expected_make_build_command = (
        'docker build --build-arg VCS_REF="$$vcs_ref" -f Dockerfile.api '
        + "-t auto_trader-api:local ."
    )
    assert expected_make_build_command in makefile
    assert "docker-run: docker-build ## Run Docker container" in makefile


def test_local_make_docker_targets_share_local_image_tag() -> None:
    makefile = _read("Makefile")

    assert (
        'docker build --build-arg VCS_REF="$$vcs_ref" -f Dockerfile.api '
        + "-t auto_trader-api:local ."
    ) in makefile
    assert "docker-run: docker-build ## Run Docker container" in makefile
    assert (
        "docker run --rm --env-file .env -p 8000:8000 auto_trader-api:local" in makefile
    )
    assert "docker-test: docker-build ## Run tests in Docker" in makefile
    assert "docker run --rm auto_trader-api:local uv run pytest tests/ -v" in makefile


def test_local_docker_docs_show_vcs_ref_aware_commands() -> None:
    docker_usage = _read("DOCKER_USAGE.md")

    assert "./run_api_compose.sh" in docker_usage
    assert "./run_docker.sh" in docker_usage
    expected_api_compose_command = (
        "VCS_REF=$(git rev-parse HEAD) docker compose -f "
        + "docker-compose.api.yml up -d --build"
    )
    assert expected_api_compose_command in docker_usage
    expected_full_compose_command = (
        "VCS_REF=$(git rev-parse HEAD) docker compose -f "
        + "docker-compose.full.yml up -d --build"
    )
    assert expected_full_compose_command in docker_usage
    expected_direct_build_example = (
        'docker build --build-arg VCS_REF="$(git rev-parse HEAD)" '
        + "-f Dockerfile.api -t auto_trader-api:local ."
    )
    assert expected_direct_build_example in docker_usage
    expected_rebuild_command = (
        "VCS_REF=$(git rev-parse HEAD) docker compose -f docker-compose.api.yml up "
        + "-d --build --force-recreate"
    )
    assert expected_rebuild_command in docker_usage
