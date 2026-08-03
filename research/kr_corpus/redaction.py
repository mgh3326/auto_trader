"""Credential loading and strict no-leak output handling for pykrx.

pykrx 1.2.8 prints the KRX login ID while constructing its session.  The
collector therefore loads the dedicated credentials only in-process and wraps
all pykrx imports/calls in an in-memory stdout/stderr sink.  Captured content
is redacted and then discarded; it is never added to an artifact or progress
log.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_CREDENTIAL_KEYS = ("KRX_ID", "KRX_PW")


class CredentialPreconditionError(RuntimeError):
    """The dedicated research credential file is unavailable or incomplete."""


@dataclass(frozen=True)
class Redactor:
    """Redacts the exact credential values from any diagnostic text."""

    secrets: tuple[str, ...]

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in sorted((s for s in self.secrets if s), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read only basic KEY=VALUE entries without shell evaluation.

    This intentionally does not ``source`` the file, avoiding command
    substitution and preventing shell output from exposing credentials.
    """
    if not path.is_file():
        raise CredentialPreconditionError(
            "dedicated research credential file is absent"
        )

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator or not _ENV_KEY.fullmatch(key.strip()):
            continue
        normalized = value.strip()
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0]
            in {
                "'",
                '"',
            }
        ):
            normalized = normalized[1:-1]
        values[key.strip()] = normalized
    return values


def load_dedicated_credentials(path: Path) -> tuple[dict[str, str], Redactor]:
    """Load the two required variables and return a redactor, never values."""
    values = _parse_env_file(path)
    missing = [key for key in _REQUIRED_CREDENTIAL_KEYS if not values.get(key)]
    if missing:
        raise CredentialPreconditionError(
            "dedicated research credential file is missing required keys: "
            + ", ".join(missing)
        )
    credentials = {key: values[key] for key in _REQUIRED_CREDENTIAL_KEYS}
    return credentials, Redactor(tuple(credentials.values()))


@contextlib.contextmanager
def dedicated_credential_environment(
    credentials: dict[str, str],
) -> Iterator[None]:
    """Expose credentials only while pykrx is running in this process."""
    previous = {key: os.environ.get(key) for key in credentials}
    os.environ.update(credentials)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@contextlib.contextmanager
def discard_redacted_source_output(redactor: Redactor) -> Iterator[None]:
    """Capture, redact, and discard pykrx stdout/stderr on every exit path."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            yield
    finally:
        # The calls prove redaction is applied before disposal.  Do not retain
        # the resulting strings: even redacted source output does not belong in
        # a corpus artifact, report, or progress event.
        redactor.redact(stdout.getvalue())
        redactor.redact(stderr.getvalue())
        stdout.close()
        stderr.close()
