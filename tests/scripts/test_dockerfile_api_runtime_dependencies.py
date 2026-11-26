from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_api_image_installs_runtime_dependencies_with_compiled_bytecode():
    dockerfile = (ROOT / "Dockerfile.api").read_text()
    sync_line = next(line for line in dockerfile.splitlines() if "uv sync" in line)

    assert "--no-dev" in sync_line
    assert "--compile-bytecode" in sync_line
    assert "PYTHONHASHSEED=0" in sync_line
    assert "SOURCE_DATE_EPOCH=0" in sync_line

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    runtime_dependencies = set(project["dependencies"])
    assert "jsonschema>=4.26.0" in runtime_dependencies
    assert "rfc3339-validator>=0.1.4" in runtime_dependencies
