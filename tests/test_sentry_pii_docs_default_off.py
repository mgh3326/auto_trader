"""ROB-1305 runtime default is SENTRY_SEND_DEFAULT_PII=False
(app/core/config.py). Several env examples and docs still advertise
SENTRY_SEND_DEFAULT_PII=true as the recommended/example value, which would
lead an operator copy-pasting the example straight back into PII collection.
This test pins every committed env example and doc to advertise the actual
default (false / off), and pins MCP prompt/result collection default-off
alongside it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every file that documents SENTRY_SEND_DEFAULT_PII as a setting an operator
# would read or copy. If one of these advertises `=true` (or Korean prose
# saying "PII collection stays on"), that's the ROB-1305 doc drift this test
# closes.
_SENTRY_PII_DOC_FILES = [
    "env.example",
    "env.prod.example",
    "MONITORING_README.md",
    "ERROR_REPORTING_README.md",
    "LOGGING_SETUP.md",
    "DEPLOYMENT.md",
]

_PII_TRUE_RE = re.compile(r"SENTRY_SEND_DEFAULT_PII\s*=\s*true", re.IGNORECASE)
_MCP_PROMPTS_TRUE_RE = re.compile(
    r"SENTRY_MCP_INCLUDE_PROMPTS\s*=\s*true", re.IGNORECASE
)
_MCP_PROMPTS_FALSE_RE = re.compile(
    r"SENTRY_MCP_INCLUDE_PROMPTS\s*=\s*false", re.IGNORECASE
)


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _SENTRY_PII_DOC_FILES)
def test_doc_does_not_advertise_pii_default_on(relative_path):
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    match = _PII_TRUE_RE.search(text)
    assert match is None, (
        f"{relative_path} advertises SENTRY_SEND_DEFAULT_PII=true, but the "
        "runtime default (app/core/config.py SENTRY_SEND_DEFAULT_PII) is "
        "False (ROB-1305) — this doc/example must show the real default"
    )


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _SENTRY_PII_DOC_FILES)
def test_doc_does_not_advertise_mcp_prompts_default_on(relative_path):
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    match = _MCP_PROMPTS_TRUE_RE.search(text)
    assert match is None, (
        f"{relative_path} advertises SENTRY_MCP_INCLUDE_PROMPTS=true; MCP "
        "prompt/result content collection must stay default-off"
    )


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", _SENTRY_PII_DOC_FILES)
def test_doc_positively_advertises_mcp_prompts_default_off(relative_path):
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert _MCP_PROMPTS_FALSE_RE.search(text), (
        f"{relative_path} must explicitly advertise "
        "SENTRY_MCP_INCLUDE_PROMPTS=false so the default-off contract is "
        "copyable and reviewable"
    )


@pytest.mark.unit
def test_monitoring_readme_prose_does_not_claim_pii_stays_on():
    text = (_REPO_ROOT / "MONITORING_README.md").read_text(encoding="utf-8")
    assert "send_default_pii=true" not in text.lower()


@pytest.mark.unit
def test_config_runtime_default_is_still_false():
    """Preservation guard: this test suite assumes the ROB-1305 runtime
    default; if that ever regresses, the doc-drift tests above would start
    asserting the wrong thing silently."""
    from app.core.config import Settings

    field = Settings.model_fields["SENTRY_SEND_DEFAULT_PII"]
    assert field.default is False
