"""Small, dependency-free producer for durable panewire lane events."""

from __future__ import annotations

import os
import re
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

LANE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LANE_EVENT_TEXT_LIMIT = 2048


@dataclass(frozen=True)
class LaneEventConfig:
    binary: str = "panewire"
    host: str = ""
    pane: str = ""
    inbox_root: str = ""
    timeout_s: float = 3.0


@dataclass(frozen=True)
class LaneEmitResult:
    outcome: str
    returncode: int | None
    reason: str | None


def lane_event_config_from_env(
    *,
    prefix_fallbacks: Sequence[str],
    pane_none_values: frozenset[str] = frozenset(),
) -> LaneEventConfig:
    """Load a lane-event emitter configuration from ordered env namespaces.

    The first namespace that names a value wins.  This lets an established
    producer retain its dedicated settings while newer producers share the
    ``LANE_EVENT_EMIT_*`` defaults.
    """
    if not prefix_fallbacks:
        raise ValueError("at least one lane-event env prefix is required")

    def value(name: str, default: str) -> tuple[str, str | None]:
        for prefix in prefix_fallbacks:
            configured = os.getenv(f"{prefix}_{name}")
            if configured is not None:
                return configured, prefix
        return default, None

    timeout_value, timeout_prefix = value("TIMEOUT_S", "3")
    timeout_s = float(timeout_value)
    if timeout_s <= 1:
        raise ValueError(
            f"{timeout_prefix or prefix_fallbacks[0]}_TIMEOUT_S must be greater than 1"
        )
    pane, _ = value("PANE", "")
    if pane in pane_none_values:
        pane = ""
    binary, _ = value("BIN", "panewire")
    host, _ = value("HOST", socket.gethostname())
    inbox_root, _ = value("INBOX_ROOT", str(Path("~/work/herdr-inbox").expanduser()))
    return LaneEventConfig(
        binary=binary,
        host=host,
        pane=pane,
        inbox_root=inbox_root,
        timeout_s=timeout_s,
    )


def _contains_control(text: str) -> bool:
    return any(ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in text)


def _valid_event_id(event_id: str) -> bool:
    if not event_id or _contains_control(event_id):
        return False
    try:
        return len(event_id.encode("utf-8")) <= 512
    except UnicodeError:
        return False


def sanitize_lane_event_text(text: str) -> str:
    """Make text acceptable to panewire without relying on its truncation."""
    cleaned = "".join(" " if _contains_control(char) else char for char in text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) <= LANE_EVENT_TEXT_LIMIT:
        return encoded.decode("utf-8")
    return encoded[:LANE_EVENT_TEXT_LIMIT].decode("utf-8", errors="ignore")


def emit_lane_event(
    lane: str,
    event_id: str,
    text: str,
    *,
    config: LaneEventConfig,
    command: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> LaneEmitResult:
    """Emit one idempotent lane event and collapse every failure to a safe result."""
    if not LANE_PATTERN.fullmatch(lane):
        return LaneEmitResult("failed", None, "invalid_lane")
    if not _valid_event_id(event_id):
        return LaneEmitResult("failed", None, "invalid_event_id")

    sanitized = sanitize_lane_event_text(text)
    if not sanitized:
        return LaneEmitResult("failed", None, "empty_text")

    host = config.host or socket.gethostname()
    inbox_root = config.inbox_root or str(Path("~/work/herdr-inbox").expanduser())
    timeout = f"{max(1, int(config.timeout_s) - 1)}s"
    argv = [
        config.binary,
        "emit",
        "--kind",
        "lane.event",
        "--lane",
        lane,
        "--event-id",
        event_id,
        "--text",
        sanitized,
        "--host",
        host,
    ]
    if config.pane:
        argv.extend(("--pane", config.pane))
    argv.extend(("--inbox-root", inbox_root, "--timeout", timeout))

    def run(command_argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command_argv),
            text=True,
            capture_output=True,
            check=False,
            timeout=config.timeout_s,
        )

    try:
        completed = (command or run)(argv)
    except FileNotFoundError:
        return LaneEmitResult("failed", None, "binary_not_found")
    except subprocess.TimeoutExpired:
        return LaneEmitResult("failed", None, "timeout")
    except (OSError, subprocess.SubprocessError):
        return LaneEmitResult("failed", None, "os_error")
    except Exception:  # noqa: BLE001 - lane delivery is always best effort
        return LaneEmitResult("failed", None, "os_error")

    if completed.returncode == 0:
        return LaneEmitResult("emitted", completed.returncode, None)
    if completed.returncode == 2 and "duplicate event_id" in (completed.stderr or ""):
        return LaneEmitResult("duplicate", completed.returncode, None)
    if completed.returncode == 2:
        return LaneEmitResult("failed", completed.returncode, "usage")
    return LaneEmitResult(
        "failed", completed.returncode, f"exit_{completed.returncode}"
    )
