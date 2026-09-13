"""Hermetic B0X observation/replay capabilities.

Every input is a caller-supplied artifact beneath an explicit root. There is
no settings, credential, database, network, process, account, or trading
dependency in this module. The schemas mirror the PR49 witness contract rather
than accepting arbitrary dictionaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.services.b0x_lane_consumer import validate_source_event

MAX_ARTIFACT_BYTES = 1_000_000
DETERMINISTIC_COMPARISON_DOMAINS = (
    "permission",
    "account",
    "target",
    "intent",
    "quantity_or_price_band",
    "guard_decisions",
)
NONDETERMINISTIC_EXCLUSION_VOCABULARY = {
    "emitted_at": "temporal",
    "observed_at": "temporal",
    "completed_at": "temporal",
    "report_prose": "prose",
    "diagnostic_prose": "prose",
}
_ZERO_AUTHORITY_KEYS = frozenset({"order", "orders", "action", "actions"})
_HEX_40_OR_64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class ShadowReplayContractError(ValueError):
    """A supplied artifact violates the observation-only boundary."""


def _root(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ShadowReplayContractError("artifact_root must be absolute")
    if path.is_symlink() or not path.is_dir():
        raise ShadowReplayContractError("artifact_root must be a real directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ShadowReplayContractError("artifact_root must be canonical")
    return resolved


def _artifact(artifact_root: str, relative_path: str, *, write: bool) -> Path:
    root = _root(artifact_root)
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ShadowReplayContractError("artifact path must be safe and relative")
    target = root.joinpath(relative)
    parent = target.parent.resolve(strict=True)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ShadowReplayContractError("artifact path escapes artifact_root") from exc
    if parent != target.parent.absolute():
        raise ShadowReplayContractError("artifact parent must not traverse a symlink")
    if target.is_symlink():
        raise ShadowReplayContractError("artifact must not be a symlink")
    if not write and not target.is_file():
        raise ShadowReplayContractError("artifact must be an existing file")
    return target


def _bytes(artifact_root: str, relative_path: str) -> bytes:
    target = _artifact(artifact_root, relative_path, write=False)
    size = target.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ShadowReplayContractError("artifact exceeds size limit")
    return target.read_bytes()


def _json(artifact_root: str, relative_path: str) -> dict[str, Any]:
    try:
        value = json.loads(_bytes(artifact_root, relative_path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowReplayContractError("artifact must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ShadowReplayContractError("artifact must be a JSON object")
    _assert_zero_authority(value)
    return value


def _observation(result: dict[str, Any]) -> dict[str, Any]:
    _assert_zero_authority(result)
    return {"dry_run": True, "orders": [], "actions": [], **result}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _strict_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise ShadowReplayContractError(
            f"{label} field set differs: missing={missing}, extra={extra}"
        )


def _canonical_text(value: Any, label: str, *, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ShadowReplayContractError(f"{label} must be non-empty canonical text")
    if prefix is not None and not value.startswith(prefix):
        raise ShadowReplayContractError(f"{label} must start with {prefix!r}")
    return value


def _assert_zero_authority(value: Any, path: str = "result") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            name = str(key)
            if name.lower() in _ZERO_AUTHORITY_KEYS and nested != []:
                raise ShadowReplayContractError(
                    f"{path}.{name} must be an empty list in shadow/replay artifacts"
                )
            _assert_zero_authority(nested, f"{path}.{name}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_zero_authority(nested, f"{path}[{index}]")


def _source_event(
    artifact_root: str, relative_path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    event = _json(artifact_root, relative_path)
    observed = validate_source_event(event)
    return event, observed


def _session_context(artifact_root: str, relative_path: str) -> dict[str, Any]:
    value = _json(artifact_root, relative_path)
    expected = {
        "kind",
        "artifact_ref",
        "owner",
        "provenance_ref",
        "source_event_id",
        "input_snapshot_ref",
        "context",
        "orders",
        "actions",
    }
    _strict_keys(value, expected, "canonical session context")
    if value["kind"] != "canonical_session_context":
        raise ShadowReplayContractError("artifact is not canonical session context")
    _canonical_text(
        value["artifact_ref"],
        "session context artifact_ref",
        prefix="session_context://canonical/",
    )
    _canonical_text(value["owner"], "session context owner")
    _canonical_text(value["provenance_ref"], "session context provenance_ref")
    _canonical_text(value["source_event_id"], "session context source_event_id")
    _canonical_text(value["input_snapshot_ref"], "session context input_snapshot_ref")
    if not isinstance(value["context"], Mapping):
        raise ShadowReplayContractError("session context payload must be a mapping")
    if value["orders"] != [] or value["actions"] != []:
        raise ShadowReplayContractError("session context has order/action authority")
    return value


def _input_snapshot(artifact_root: str, relative_path: str) -> dict[str, Any]:
    value = _json(artifact_root, relative_path)
    expected = {
        "kind",
        "artifact_ref",
        "availability_proof_ref",
        "available",
        "source_event_id",
        "inputs",
        "orders",
        "actions",
    }
    _strict_keys(value, expected, "identical input snapshot")
    if value["kind"] != "identical_input_snapshot":
        raise ShadowReplayContractError("artifact is not an identical input snapshot")
    _canonical_text(
        value["artifact_ref"], "input snapshot artifact_ref", prefix="artifact://input/"
    )
    _canonical_text(
        value["availability_proof_ref"],
        "input snapshot availability_proof_ref",
        prefix="artifact://input/",
    )
    _canonical_text(value["source_event_id"], "input snapshot source_event_id")
    if value["available"] is not True:
        raise ShadowReplayContractError("identical input must be explicitly available")
    if not isinstance(value["inputs"], Mapping):
        raise ShadowReplayContractError("identical input payload must be a mapping")
    if value["orders"] != [] or value["actions"] != []:
        raise ShadowReplayContractError("identical input has order/action authority")
    return value


def _playbook(
    artifact_root: str,
    relative_path: str,
    *,
    source_event_relative_path: str,
) -> tuple[str, str, dict[str, Any]]:
    if not relative_path.endswith(".md"):
        raise ShadowReplayContractError("playbook artifact must be Markdown")
    raw = _bytes(artifact_root, relative_path)
    try:
        contents = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShadowReplayContractError("playbook must be UTF-8") from exc
    if not contents.strip():
        raise ShadowReplayContractError("playbook must not be empty")
    event, observed = _source_event(artifact_root, source_event_relative_path)
    body = json.loads(str(event["text"]))
    if body.get("playbook") != relative_path:
        raise ShadowReplayContractError(
            "emitted source event does not bind the requested playbook"
        )
    return contents, hashlib.sha256(raw).hexdigest(), observed


def canonical_session_context_read(
    artifact_root: str, relative_path: str
) -> dict[str, Any]:
    value = _session_context(artifact_root, relative_path)
    return _observation({"context": value, "sha256": _digest(value)})


def source_event_observe(artifact_root: str, relative_path: str) -> dict[str, Any]:
    _, observed = _source_event(artifact_root, relative_path)
    return _observation({"observed": observed, "consumed": False})


def emitted_trigger_playbook_read(
    artifact_root: str,
    relative_path: str,
    source_event_relative_path: str,
) -> dict[str, Any]:
    contents, digest, observed = _playbook(
        artifact_root,
        relative_path,
        source_event_relative_path=source_event_relative_path,
    )
    return _observation(
        {
            "playbook": contents,
            "sha256": digest,
            "source_event_id": observed["event_id"],
            "playbook_ref": relative_path,
        }
    )


def identical_input_snapshot_read(
    artifact_root: str, relative_path: str
) -> dict[str, Any]:
    value = _input_snapshot(artifact_root, relative_path)
    return _observation({"snapshot": value, "sha256": _digest(value)})


def _write_new(target: Path, value: dict[str, Any]) -> str:
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ShadowReplayContractError("artifact exceeds size limit")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


def _live_output_provenance(value: dict[str, Any]) -> dict[str, str]:
    expected = {
        "artifact_ref",
        "owner",
        "provenance_ref",
        "session_context_ref",
        "session_context_owner",
        "session_context_provenance_ref",
    }
    _strict_keys(value, expected, "live output provenance")
    return {
        "artifact_ref": _canonical_text(
            value["artifact_ref"], "live artifact_ref", prefix="artifact://legacy/"
        ),
        "owner": _canonical_text(value["owner"], "live owner"),
        "provenance_ref": _canonical_text(
            value["provenance_ref"], "live provenance_ref"
        ),
        "session_context_ref": _canonical_text(
            value["session_context_ref"],
            "live session_context_ref",
            prefix="session_context://canonical/legacy/",
        ),
        "session_context_owner": _canonical_text(
            value["session_context_owner"], "live session_context_owner"
        ),
        "session_context_provenance_ref": _canonical_text(
            value["session_context_provenance_ref"],
            "live session_context_provenance_ref",
        ),
    }


def shadow_report_write(
    artifact_root: str,
    relative_path: str,
    report: dict[str, Any],
    provenance: dict[str, Any],
    live_output_provenance: dict[str, Any],
    source_event_relative_path: str,
    playbook_relative_path: str,
    input_snapshot_relative_path: str,
    session_context_relative_path: str,
) -> dict[str, Any]:
    """Write a report only after all source/input/context bindings agree."""

    expected_report = {
        "source_event_id",
        "input_snapshot_ref",
        "session_context_ref",
        "playbook_ref",
        "playbook_sha256",
        "deterministic_values",
        "orders",
        "actions",
    }
    expected_provenance = {
        "artifact_ref",
        "owner",
        "provenance_ref",
        "source_event_id",
        "input_snapshot_ref",
        "input_sha256",
        "session_context_ref",
        "playbook_sha256",
        "code_sha",
    }
    _strict_keys(report, expected_report, "shadow report")
    _strict_keys(provenance, expected_provenance, "shadow report provenance")
    _assert_zero_authority(report)
    if report["orders"] != [] or report["actions"] != []:
        raise ShadowReplayContractError("shadow report has order/action authority")

    _, observed = _source_event(artifact_root, source_event_relative_path)
    _, playbook_sha256, playbook_observed = _playbook(
        artifact_root,
        playbook_relative_path,
        source_event_relative_path=source_event_relative_path,
    )
    snapshot = _input_snapshot(artifact_root, input_snapshot_relative_path)
    context = _session_context(artifact_root, session_context_relative_path)
    event_id = str(observed["event_id"])
    if playbook_observed["event_id"] != event_id:
        raise ShadowReplayContractError("playbook and source event identities differ")
    if (
        snapshot["source_event_id"] != event_id
        or context["source_event_id"] != event_id
    ):
        raise ShadowReplayContractError("context/input/source event identities differ")
    if context["input_snapshot_ref"] != snapshot["artifact_ref"]:
        raise ShadowReplayContractError(
            "context does not bind identical input snapshot"
        )

    domains = report["deterministic_values"]
    if not isinstance(domains, Mapping):
        raise ShadowReplayContractError("deterministic_values must be a mapping")
    _strict_keys(domains, set(DETERMINISTIC_COMPARISON_DOMAINS), "deterministic_values")
    expected_bindings = {
        "source_event_id": event_id,
        "input_snapshot_ref": snapshot["artifact_ref"],
        "session_context_ref": context["artifact_ref"],
        "playbook_ref": playbook_relative_path,
        "playbook_sha256": playbook_sha256,
    }
    for field, expected in expected_bindings.items():
        if report[field] != expected:
            raise ShadowReplayContractError(f"shadow report {field} binding differs")
    if provenance["source_event_id"] != event_id:
        raise ShadowReplayContractError("shadow provenance source_event_id differs")
    if provenance["input_snapshot_ref"] != snapshot["artifact_ref"]:
        raise ShadowReplayContractError("shadow provenance input_snapshot_ref differs")
    if provenance["input_sha256"] != _digest(snapshot):
        raise ShadowReplayContractError("shadow provenance input_sha256 differs")
    if provenance["session_context_ref"] != context["artifact_ref"]:
        raise ShadowReplayContractError("shadow provenance session_context_ref differs")
    if provenance["playbook_sha256"] != playbook_sha256:
        raise ShadowReplayContractError("shadow provenance playbook_sha256 differs")
    if not isinstance(provenance["code_sha"], str) or not _HEX_40_OR_64.fullmatch(
        provenance["code_sha"]
    ):
        raise ShadowReplayContractError("shadow provenance code_sha must be 40/64 hex")
    shadow_artifact = _canonical_text(
        provenance["artifact_ref"],
        "shadow artifact_ref",
        prefix="artifact://resident-shadow/",
    )
    shadow_owner = _canonical_text(provenance["owner"], "shadow owner")
    shadow_provenance = _canonical_text(
        provenance["provenance_ref"], "shadow provenance_ref"
    )
    live = _live_output_provenance(live_output_provenance)
    for label, shadow_value, live_value in (
        ("report artifact", shadow_artifact, live["artifact_ref"]),
        ("report owner", shadow_owner, live["owner"]),
        ("report provenance", shadow_provenance, live["provenance_ref"]),
        (
            "session context artifact",
            context["artifact_ref"],
            live["session_context_ref"],
        ),
        ("session context owner", context["owner"], live["session_context_owner"]),
        (
            "session context provenance",
            context["provenance_ref"],
            live["session_context_provenance_ref"],
        ),
    ):
        if shadow_value == live_value:
            raise ShadowReplayContractError(f"live/shadow {label} must be distinct")

    payload = {
        "kind": "b0x_shadow_report",
        "owner": shadow_owner,
        "artifact_ref": shadow_artifact,
        "provenance": provenance,
        "live_output_provenance": live,
        "dry_run": True,
        "orders": [],
        "actions": [],
        "report": report,
    }
    target = _artifact(artifact_root, relative_path, write=True)
    digest = _write_new(target, payload)
    return _observation(
        {
            "written": True,
            "artifact_ref": shadow_artifact,
            "relative_path": relative_path,
            "sha256": digest,
        }
    )


def _replay_snapshot(value: dict[str, Any], *, role: str) -> dict[str, Any]:
    expected = {
        "kind",
        "artifact_ref",
        "owner",
        "provenance_ref",
        "session_context_ref",
        "session_context_owner",
        "session_context_provenance_ref",
        "source_event_id",
        "input_snapshot_ref",
        "domains",
        "orders",
        "actions",
    }
    _strict_keys(value, expected, f"{role} replay snapshot")
    if value["kind"] != f"b0x_{role}_replay_snapshot":
        raise ShadowReplayContractError(f"{role} replay snapshot kind differs")
    prefix = "artifact://legacy/" if role == "live" else "artifact://resident-shadow/"
    context_prefix = (
        "session_context://canonical/legacy/"
        if role == "live"
        else "session_context://canonical/resident-shadow/"
    )
    for field in ("owner", "provenance_ref", "source_event_id", "input_snapshot_ref"):
        _canonical_text(value[field], f"{role} {field}")
    _canonical_text(value["artifact_ref"], f"{role} artifact_ref", prefix=prefix)
    _canonical_text(
        value["session_context_ref"],
        f"{role} session_context_ref",
        prefix=context_prefix,
    )
    _canonical_text(value["session_context_owner"], f"{role} session_context_owner")
    _canonical_text(
        value["session_context_provenance_ref"],
        f"{role} session_context_provenance_ref",
    )
    domains = value["domains"]
    if not isinstance(domains, Mapping):
        raise ShadowReplayContractError(f"{role} domains must be a mapping")
    _strict_keys(domains, set(DETERMINISTIC_COMPARISON_DOMAINS), f"{role} domains")
    _assert_zero_authority(value)
    if value["orders"] != [] or value["actions"] != []:
        raise ShadowReplayContractError(f"{role} snapshot has order/action results")
    return value


def _bound_replay_comparison(
    live_snapshot: dict[str, Any], shadow_snapshot: dict[str, Any]
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
]:
    live = _replay_snapshot(live_snapshot, role="live")
    shadow = _replay_snapshot(shadow_snapshot, role="shadow")
    for field in ("source_event_id", "input_snapshot_ref"):
        if live[field] != shadow[field]:
            raise ShadowReplayContractError(
                f"live/shadow {field} must bind the same replay input"
            )
    for label, field in (
        ("report artifact", "artifact_ref"),
        ("report owner", "owner"),
        ("report provenance", "provenance_ref"),
        ("session context artifact", "session_context_ref"),
        ("session context owner", "session_context_owner"),
        ("session context provenance", "session_context_provenance_ref"),
    ):
        if live[field] == shadow[field]:
            raise ShadowReplayContractError(f"live/shadow {label} must be distinct")

    comparison = {
        field: {
            "live": live["domains"][field],
            "shadow": shadow["domains"][field],
            "match": live["domains"][field] == shadow["domains"][field],
        }
        for field in DETERMINISTIC_COMPARISON_DOMAINS
    }
    differences = [
        {"field": field, **values}
        for field, values in comparison.items()
        if values["match"] is not True
    ]
    return live, shadow, comparison, differences


def deterministic_replay_compare(
    live_snapshot: dict[str, Any], shadow_snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Compare exactly the six deterministic PR49 domains."""

    live, shadow, comparison, differences = _bound_replay_comparison(
        live_snapshot, shadow_snapshot
    )
    return _observation(
        {
            "deterministic": not differences,
            "deterministic_comparison": comparison,
            "differences": differences,
            # Deterministic-domain drift cannot be relabelled as temporal or
            # prose. Every such mismatch is therefore unexplained.
            "unexplained_difference_count": len(differences),
            "live_sha256": _digest(live),
            "shadow_sha256": _digest(shadow),
            "comparison_sha256": _digest(comparison),
            "source_event_id": live["source_event_id"],
            "input_snapshot_ref": live["input_snapshot_ref"],
        }
    )


def raw_difference_artifact_write(
    artifact_root: str,
    relative_path: str,
    differences: dict[str, dict[str, Any]],
    classifications: dict[str, str],
    provenance: dict[str, Any],
    live_snapshot: dict[str, Any],
    shadow_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Persist every classified raw difference with complete provenance."""

    if not isinstance(differences, Mapping) or not isinstance(classifications, Mapping):
        raise ShadowReplayContractError("differences and classifications are required")
    if set(differences) != set(classifications):
        raise ShadowReplayContractError(
            "raw differences must exactly match classified nondeterministic fields"
        )
    if set(differences).intersection(DETERMINISTIC_COMPARISON_DOMAINS):
        raise ShadowReplayContractError(
            "raw differences must not reclassify deterministic domains"
        )
    live, shadow, comparison, deterministic_differences = _bound_replay_comparison(
        live_snapshot, shadow_snapshot
    )
    if deterministic_differences:
        raise ShadowReplayContractError(
            "raw difference artifact requires zero unexplained deterministic differences"
        )
    for field, reason in classifications.items():
        if NONDETERMINISTIC_EXCLUSION_VOCABULARY.get(field) != reason:
            raise ShadowReplayContractError(
                "classification is outside the fixed temporal/prose vocabulary"
            )
        values = differences[field]
        if not isinstance(values, Mapping) or set(values) != {"live", "shadow"}:
            raise ShadowReplayContractError(
                f"raw difference {field!r} must preserve exact live/shadow values"
            )
    _assert_zero_authority(differences)
    expected_provenance = {
        "artifact_ref",
        "owner",
        "provenance_ref",
        "source_event_id",
        "input_snapshot_ref",
        "live_artifact_ref",
        "live_owner",
        "live_provenance_ref",
        "shadow_artifact_ref",
        "shadow_owner",
        "shadow_provenance_ref",
        "comparison_sha256",
    }
    _strict_keys(provenance, expected_provenance, "raw difference provenance")
    raw_artifact = _canonical_text(
        provenance["artifact_ref"],
        "raw difference artifact_ref",
        prefix="artifact://resident-shadow/",
    )
    raw_owner = _canonical_text(provenance["owner"], "raw difference owner")
    raw_provenance = _canonical_text(
        provenance["provenance_ref"], "raw difference provenance_ref"
    )
    for field in ("source_event_id", "input_snapshot_ref"):
        _canonical_text(provenance[field], f"raw difference {field}")
    _canonical_text(
        provenance["live_artifact_ref"],
        "raw difference live_artifact_ref",
        prefix="artifact://legacy/",
    )
    _canonical_text(
        provenance["shadow_artifact_ref"],
        "raw difference shadow_artifact_ref",
        prefix="artifact://resident-shadow/",
    )
    for field in (
        "live_owner",
        "live_provenance_ref",
        "shadow_owner",
        "shadow_provenance_ref",
    ):
        _canonical_text(provenance[field], f"raw difference {field}")
    if not isinstance(provenance["comparison_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", provenance["comparison_sha256"]
    ):
        raise ShadowReplayContractError(
            "raw difference comparison_sha256 must be 64 hex"
        )
    bound_provenance = {
        "source_event_id": live["source_event_id"],
        "input_snapshot_ref": live["input_snapshot_ref"],
        "live_artifact_ref": live["artifact_ref"],
        "live_owner": live["owner"],
        "live_provenance_ref": live["provenance_ref"],
        "shadow_artifact_ref": shadow["artifact_ref"],
        "shadow_owner": shadow["owner"],
        "shadow_provenance_ref": shadow["provenance_ref"],
        "comparison_sha256": _digest(comparison),
    }
    for field, expected in bound_provenance.items():
        if provenance[field] != expected:
            raise ShadowReplayContractError(
                f"raw difference provenance {field} is not bound to replay snapshots"
            )
    for label, left, right in (
        ("live/shadow artifact", "live_artifact_ref", "shadow_artifact_ref"),
        ("live/shadow owner", "live_owner", "shadow_owner"),
        ("live/shadow provenance", "live_provenance_ref", "shadow_provenance_ref"),
    ):
        if provenance[left] == provenance[right]:
            raise ShadowReplayContractError(f"{label} must be distinct")
    if raw_artifact in {
        provenance["live_artifact_ref"],
        provenance["shadow_artifact_ref"],
    }:
        raise ShadowReplayContractError("raw difference artifact must be independent")
    if raw_provenance in {
        provenance["live_provenance_ref"],
        provenance["shadow_provenance_ref"],
    }:
        raise ShadowReplayContractError("raw difference provenance must be independent")
    if raw_owner == provenance["live_owner"]:
        raise ShadowReplayContractError(
            "raw difference owner must not alias live owner"
        )
    if raw_owner != provenance["shadow_owner"]:
        raise ShadowReplayContractError(
            "raw difference owner must be the independent shadow owner"
        )

    payload = {
        "kind": "b0x_raw_replay_difference",
        "artifact_ref": raw_artifact,
        "owner": raw_owner,
        "provenance": provenance,
        "dry_run": True,
        "orders": [],
        "actions": [],
        "classifications": dict(classifications),
        "differences": dict(differences),
    }
    target = _artifact(artifact_root, relative_path, write=True)
    digest = _write_new(target, payload)
    return _observation(
        {
            "written": True,
            "artifact_ref": raw_artifact,
            "relative_path": relative_path,
            "sha256": digest,
        }
    )


__all__ = [
    "DETERMINISTIC_COMPARISON_DOMAINS",
    "NONDETERMINISTIC_EXCLUSION_VOCABULARY",
    "ShadowReplayContractError",
    "canonical_session_context_read",
    "deterministic_replay_compare",
    "emitted_trigger_playbook_read",
    "identical_input_snapshot_read",
    "raw_difference_artifact_write",
    "shadow_report_write",
    "source_event_observe",
]
