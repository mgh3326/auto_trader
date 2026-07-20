"""ROB-984 H6-B directory-atomic scorecard artifact primitive.

The primitive owns physical durability only.  H5 remains the sole authority
for canonical JSON bytes, semantic scorecard hashing, and Markdown rendering,
supplied through a narrow pure port until the verified H5 merge is available.

The JSON/Markdown pair becomes visible through one no-replace directory
rename.  PostgreSQL commit and this rename are intentionally not described as
cross-resource atomic.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "ArtifactCollisionError",
    "ArtifactError",
    "ArtifactForensicState",
    "ArtifactPresence",
    "ArtifactPreflightError",
    "ArtifactPublishError",
    "ArtifactReplayInspection",
    "ArtifactStageError",
    "ArtifactVerificationError",
    "H5ArtifactPort",
    "PublishedArtifactPair",
    "StagedArtifactPair",
    "inspect_exact_artifact_replay",
    "probe_artifact_state",
    "publish_staged_pair",
    "stage_scorecard_pair",
]

_JSON_NAME = "scorecard.json"
_MARKDOWN_NAME = "scorecard.md"
_EXACT_NAMES = frozenset({_JSON_NAME, _MARKDOWN_NAME})
_HEX = frozenset("0123456789abcdef")


class H5ArtifactPort(Protocol):
    """Pure H5 seam; H6-B neither defaults nor recomputes scorecard meaning."""

    provenance: str

    def canonical_json_bytes(self, scorecard: Mapping[str, object]) -> bytes: ...

    def semantic_hash(self, scorecard: Mapping[str, object]) -> str: ...

    def render_markdown(self, scorecard: Mapping[str, object]) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ArtifactForensicState:
    stage: str
    staging_dir: Path | None
    final_dir: Path
    renamed: bool


class ArtifactError(RuntimeError):
    """Base physical artifact failure."""


class ArtifactPreflightError(ArtifactError):
    """Path/shape/port validation failed before staging."""


class ArtifactCollisionError(ArtifactError):
    """Existing output/staging/concurrent creator is never overwritten."""


class _ForensicArtifactError(ArtifactError):
    def __init__(self, message: str, state: ArtifactForensicState) -> None:
        super().__init__(message)
        self.state = state


class ArtifactStageError(_ForensicArtifactError):
    """A physical stage/fsync operation failed; staging is preserved."""


class ArtifactVerificationError(_ForensicArtifactError):
    """Physical readback, semantic hash, or renderer parity failed."""


class ArtifactPublishError(_ForensicArtifactError):
    """Rename or parent-directory fsync failed with typed forensic state."""


@dataclass(frozen=True, slots=True)
class StagedArtifactPair:
    staging_dir: Path
    final_dir: Path
    canonical_json_bytes: bytes
    markdown_bytes: bytes
    semantic_hash: str
    stage_state: str

    def __post_init__(self) -> None:
        if self.stage_state != "STAGED_FSYNCED_PHYSICALLY_VERIFIED":
            raise ValueError("invalid staged artifact state")


@dataclass(frozen=True, slots=True)
class PublishedArtifactPair:
    final_dir: Path
    json_path: Path
    markdown_path: Path
    semantic_hash: str
    stage_state: str

    def __post_init__(self) -> None:
        if self.stage_state != "PUBLISHED_PARENT_FSYNCED":
            raise ValueError("invalid published artifact state")


@dataclass(frozen=True, slots=True)
class ArtifactReplayInspection:
    final_dir: Path
    semantic_hash: str
    disposition: str

    def __post_init__(self) -> None:
        if self.disposition != "EXACT_ARTIFACT_REPLAY":
            raise ValueError("invalid replay disposition")


@dataclass(frozen=True, slots=True)
class ArtifactPresence:
    """Read-only pair/staging probe used before any database mutation."""

    state: str
    final_dir: Path
    staging_dirs: tuple[Path, ...]
    detail: str

    def __post_init__(self) -> None:
        if self.state not in {
            "ABSENT",
            "PAIR_PRESENT",
            "INVALID_FINAL",
            "STALE_STAGING",
        }:
            raise ValueError("invalid artifact presence state")


def _validate_output_path(output_dir: Path) -> tuple[Path, Path]:
    if not isinstance(output_dir, Path):
        raise ArtifactPreflightError("output_dir must be pathlib.Path")
    if not output_dir.is_absolute():
        raise ArtifactPreflightError("output_dir must be absolute")
    if ".." in output_dir.parts or output_dir.name in ("", ".", ".."):
        raise ArtifactPreflightError("path traversal or empty output name refused")
    parent = output_dir.parent
    if not parent.exists() or not parent.is_dir():
        raise ArtifactPreflightError("output parent must already exist as a directory")
    _assert_no_symlink_ancestors(parent)
    return output_dir, parent


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError as exc:
            raise ArtifactPreflightError(
                "all output-parent ancestors must already exist"
            ) from exc
        if stat.S_ISLNK(mode):
            raise ArtifactPreflightError("symlink output ancestry refused")


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _staging_prefix(output_dir: Path) -> str:
    return f".{output_dir.name}.staging-"


def _staging_siblings(output_dir: Path) -> tuple[Path, ...]:
    prefix = _staging_prefix(output_dir)
    return tuple(
        sorted(
            (
                output_dir.parent / entry.name
                for entry in os.scandir(output_dir.parent)
                if entry.name.startswith(prefix)
            ),
            key=str,
        )
    )


def _assert_no_stale_staging(output_dir: Path) -> None:
    stale = _staging_siblings(output_dir)
    if stale:
        raise ArtifactCollisionError(
            f"stale or concurrent staging path exists: {stale[0]}"
        )


def _assert_output_absent(output_dir: Path) -> None:
    if _lexists(output_dir):
        raise ArtifactCollisionError("final output already exists")


def probe_artifact_state(*, output_dir: Path) -> ArtifactPresence:
    """Classify final/staging shape without writing, deleting, or repairing."""
    output_dir, _parent = _validate_output_path(output_dir)
    staging = _staging_siblings(output_dir)
    if staging:
        return ArtifactPresence(
            state="STALE_STAGING",
            final_dir=output_dir,
            staging_dirs=staging,
            detail="one or more staging siblings already exist",
        )
    if not _lexists(output_dir):
        return ArtifactPresence(
            state="ABSENT",
            final_dir=output_dir,
            staging_dirs=(),
            detail="final pair and staging siblings are absent",
        )
    try:
        final_mode = os.lstat(output_dir).st_mode
        if not stat.S_ISDIR(final_mode) or stat.S_ISLNK(final_mode):
            raise ValueError("final path is not a regular non-symlink directory")
        entries = tuple(os.scandir(output_dir))
        names = frozenset(entry.name for entry in entries)
        if names != _EXACT_NAMES:
            raise ValueError("final directory is not the exact two-file pair")
        for entry in entries:
            mode = os.lstat(entry.path).st_mode
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise ValueError(f"{entry.name} is not a regular non-symlink file")
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        return ArtifactPresence(
            state="INVALID_FINAL",
            final_dir=output_dir,
            staging_dirs=(),
            detail=str(exc),
        )
    return ArtifactPresence(
        state="PAIR_PRESENT",
        final_dir=output_dir,
        staging_dirs=(),
        detail="exact two-file physical shape is present",
    )


def _validate_port(port: H5ArtifactPort) -> None:
    if getattr(port, "provenance", None) not in (
        "contract_fixture",
        "actual_merged_h5",
    ):
        raise ArtifactPreflightError("H5 port provenance is not explicit")
    for name in ("canonical_json_bytes", "semantic_hash", "render_markdown"):
        if not callable(getattr(port, name, None)):
            raise ArtifactPreflightError(f"H5 port lacks callable {name}")


def _validate_text_bytes(value: object, *, name: str) -> bytes:
    if type(value) is not bytes:
        raise ArtifactPreflightError(f"{name} must be exact bytes")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactPreflightError(f"{name} must be UTF-8") from exc
    if not value.endswith(b"\n") or value.endswith(b"\n\n") or b"\r" in value:
        raise ArtifactPreflightError(
            f"{name} must carry exactly one terminal LF and no CR"
        )
    return value


def _validate_semantic_hash(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ArtifactPreflightError("H5 semantic hash must be lowercase hex64")
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant refused: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key refused: {key}")
        result[key] = value
    return result


def _parse_scorecard_json(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("scorecard JSON failed strict parse") from exc
    if type(parsed) is not dict:
        raise ValueError("scorecard JSON must be an object")
    return parsed


def _build_expected_pair(
    scorecard: Mapping[str, object], port: H5ArtifactPort
) -> tuple[bytes, bytes, str]:
    if type(scorecard) is not dict:
        raise ArtifactPreflightError("scorecard must be an exact built-in dict")
    _validate_port(port)
    json_bytes = _validate_text_bytes(
        port.canonical_json_bytes(scorecard), name="canonical JSON"
    )
    # Strictly parse before touching the filesystem. This catches NaN,
    # duplicate keys, invalid UTF-8, and a non-object H5 output.
    try:
        parsed = _parse_scorecard_json(json_bytes)
    except ValueError as exc:
        raise ArtifactPreflightError(str(exc)) from exc
    if type(parsed) is not dict:
        raise ArtifactPreflightError("canonical JSON must parse to an object")
    markdown_bytes = _validate_text_bytes(
        port.render_markdown(scorecard), name="Markdown"
    )
    semantic_hash = _validate_semantic_hash(port.semantic_hash(scorecard))
    return json_bytes, markdown_bytes, semantic_hash


def _device_id(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_dev


def _write_exclusive_fsynced(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("short artifact write")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular_file(path: Path) -> bytes:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise ArtifactVerificationError(
            f"required physical artifact is absent: {path.name}",
            ArtifactForensicState("PAIR_SHAPE_FAILED", path.parent, path.parent, False),
        ) from exc
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise ArtifactVerificationError(
            f"artifact is not a regular non-symlink file: {path.name}",
            ArtifactForensicState("PAIR_SHAPE_FAILED", path.parent, path.parent, False),
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _assert_exact_pair_shape(directory: Path) -> None:
    try:
        mode = os.lstat(directory).st_mode
    except FileNotFoundError as exc:
        raise ArtifactVerificationError(
            "artifact directory is absent",
            ArtifactForensicState("PAIR_SHAPE_FAILED", directory, directory, False),
        ) from exc
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise ArtifactVerificationError(
            "artifact directory must be a non-symlink directory",
            ArtifactForensicState("PAIR_SHAPE_FAILED", directory, directory, False),
        )
    names = frozenset(entry.name for entry in os.scandir(directory))
    if names != _EXACT_NAMES:
        raise ArtifactVerificationError(
            "artifact directory must contain scorecard.json and scorecard.md only",
            ArtifactForensicState("PAIR_SHAPE_FAILED", directory, directory, False),
        )


def _verify_physical_pair(
    directory: Path,
    *,
    port: H5ArtifactPort,
    expected_json: bytes,
    expected_markdown: bytes,
    expected_semantic_hash: str,
    final_dir: Path,
    renamed: bool,
) -> None:
    state = ArtifactForensicState(
        "PHYSICAL_READBACK_FAILED",
        None if renamed else directory,
        final_dir,
        renamed,
    )
    try:
        _assert_exact_pair_shape(directory)
        physical_json = _read_regular_file(directory / _JSON_NAME)
        physical_markdown = _read_regular_file(directory / _MARKDOWN_NAME)
        if physical_json != expected_json:
            raise ValueError("physical JSON bytes differ from H5 canonical bytes")
        if physical_markdown != expected_markdown:
            raise ValueError("physical Markdown bytes differ from H5 renderer bytes")
        parsed = _parse_scorecard_json(physical_json)
        if port.canonical_json_bytes(parsed) != physical_json:
            raise ValueError("persisted JSON does not recanonicalize byte-identically")
        if port.semantic_hash(parsed) != expected_semantic_hash:
            raise ValueError("persisted JSON H5 semantic hash mismatch")
        if port.render_markdown(parsed) != physical_markdown:
            raise ValueError("persisted parsed-JSON renderer parity mismatch")
    except ArtifactVerificationError as exc:
        raise ArtifactVerificationError(str(exc), state) from exc
    except Exception as exc:
        raise ArtifactVerificationError(str(exc), state) from exc


def stage_scorecard_pair(
    *,
    scorecard: Mapping[str, object],
    output_dir: Path,
    h5_port: H5ArtifactPort,
) -> StagedArtifactPair:
    """Exclusively stage, fsync, physically reopen, hash, and render-check."""
    output_dir, parent = _validate_output_path(output_dir)
    expected_json, expected_markdown, semantic_hash = _build_expected_pair(
        scorecard, h5_port
    )
    _assert_output_absent(output_dir)
    _assert_no_stale_staging(output_dir)

    staging_dir = Path(tempfile.mkdtemp(dir=parent, prefix=_staging_prefix(output_dir)))
    state = ArtifactForensicState("STAGING_PRESERVED", staging_dir, output_dir, False)
    try:
        if _device_id(staging_dir) != _device_id(parent):
            raise ArtifactStageError("cross-filesystem staging refused", state)
        _write_exclusive_fsynced(staging_dir / _JSON_NAME, expected_json)
        _write_exclusive_fsynced(staging_dir / _MARKDOWN_NAME, expected_markdown)
        _fsync_directory(staging_dir)
        _verify_physical_pair(
            staging_dir,
            port=h5_port,
            expected_json=expected_json,
            expected_markdown=expected_markdown,
            expected_semantic_hash=semantic_hash,
            final_dir=output_dir,
            renamed=False,
        )
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactStageError("artifact staging failed", state) from exc
    return StagedArtifactPair(
        staging_dir=staging_dir,
        final_dir=output_dir,
        canonical_json_bytes=expected_json,
        markdown_bytes=expected_markdown,
        semantic_hash=semantic_hash,
        stage_state="STAGED_FSYNCED_PHYSICALLY_VERIFIED",
    )


def _rename_noreplace(source: Path, destination: Path) -> None:
    """One directory rename with kernel-enforced no-replace semantics."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        function = libc.renamex_np
        function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        result = function(source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        result = function(-100, source_bytes, -100, destination_bytes, 1)
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace directory rename unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def publish_staged_pair(
    staged: StagedArtifactPair, *, h5_port: H5ArtifactPort
) -> PublishedArtifactPair:
    """Verify once more, rename the directory once, then fsync its parent."""
    if type(staged) is not StagedArtifactPair:
        raise ArtifactPreflightError("staged must be exact StagedArtifactPair")
    _validate_port(h5_port)
    _assert_output_absent(staged.final_dir)
    siblings = _staging_siblings(staged.final_dir)
    if siblings != (staged.staging_dir,):
        raise ArtifactCollisionError("staging set changed before publication")
    _verify_physical_pair(
        staged.staging_dir,
        port=h5_port,
        expected_json=staged.canonical_json_bytes,
        expected_markdown=staged.markdown_bytes,
        expected_semantic_hash=staged.semantic_hash,
        final_dir=staged.final_dir,
        renamed=False,
    )
    before_state = ArtifactForensicState(
        "RENAME_FAILED_STAGING_PRESERVED",
        staged.staging_dir,
        staged.final_dir,
        False,
    )
    try:
        _rename_noreplace(staged.staging_dir, staged.final_dir)
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
            raise ArtifactPublishError(
                "concurrent final creator won", before_state
            ) from exc
        raise ArtifactPublishError("directory rename failed", before_state) from exc

    after_state = ArtifactForensicState(
        "PARENT_FSYNC_FAILED", None, staged.final_dir, True
    )
    try:
        _fsync_directory(staged.final_dir.parent)
    except Exception as exc:
        raise ArtifactPublishError(
            "parent-directory fsync failed", after_state
        ) from exc
    return PublishedArtifactPair(
        final_dir=staged.final_dir,
        json_path=staged.final_dir / _JSON_NAME,
        markdown_path=staged.final_dir / _MARKDOWN_NAME,
        semantic_hash=staged.semantic_hash,
        stage_state="PUBLISHED_PARENT_FSYNCED",
    )


def inspect_exact_artifact_replay(
    *,
    scorecard: Mapping[str, object],
    output_dir: Path,
    h5_port: H5ArtifactPort,
) -> ArtifactReplayInspection:
    """Read-only exact-pair inspection; never stages, removes, or publishes."""
    output_dir, _parent = _validate_output_path(output_dir)
    expected_json, expected_markdown, semantic_hash = _build_expected_pair(
        scorecard, h5_port
    )
    _assert_no_stale_staging(output_dir)
    if not _lexists(output_dir):
        raise ArtifactCollisionError("exact replay requires an existing final pair")
    _verify_physical_pair(
        output_dir,
        port=h5_port,
        expected_json=expected_json,
        expected_markdown=expected_markdown,
        expected_semantic_hash=semantic_hash,
        final_dir=output_dir,
        renamed=True,
    )
    return ArtifactReplayInspection(
        final_dir=output_dir,
        semantic_hash=semantic_hash,
        disposition="EXACT_ARTIFACT_REPLAY",
    )
