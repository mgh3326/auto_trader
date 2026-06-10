from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _copy_sources_from_dockerfile(path: Path) -> list[str]:
    sources: list[str] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY "):
            continue
        parts = line.split()
        if parts[1].startswith("--from="):
            continue
        # COPY <src...> <dest>; ignore flags and the final destination.
        src_parts = [part for part in parts[1:-1] if not part.startswith("--")]
        sources.extend(src_parts)
    return sources


def test_dockerfile_api_frontend_copy_sources_exist() -> None:
    dockerfile = REPO_ROOT / "Dockerfile.api"
    missing = [
        source
        for source in _copy_sources_from_dockerfile(dockerfile)
        if source.startswith("frontend/") and not (REPO_ROOT / source).exists()
    ]

    assert missing == []


def test_dockerfile_api_installs_invest_spa_dist_location() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile.api").read_text()

    assert "/app/frontend/invest/dist" in dockerfile
