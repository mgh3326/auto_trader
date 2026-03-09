from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_ci_test_job_excludes_integration_tests() -> None:
    workflow = _workflow_text()

    assert '-m "not live"' in workflow


def test_ci_test_job_uses_xml_coverage_only() -> None:
    workflow = _workflow_text()

    assert "--cov-report=xml" in workflow
    assert "--cov-report=html" not in workflow
    assert "files: coverage.xml" in workflow
    assert "disable_search: true" in workflow
