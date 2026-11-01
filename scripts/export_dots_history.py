#!/usr/bin/env python3
"""Export de-identified order and fill history for the dots session (ROB-1438).

Manual and read-only. It reads the order ledgers, execution_ledger (in-effect
rows only) and order proposals in one read-only transaction, replaces every
identifier with a salted hash, and writes CSV, Parquet and a manifest into a new
directory. A leak check over the rows and the written files fails the whole run.

The database is named by ``--database-url-env NAME`` so the password never
reaches the process arguments; the salt comes from ``--salt-file`` or
``--salt-env`` and must live outside the repository. Neither is ever printed.

Exit codes: 0 exported, 1 input or database error, 2 leak check failed.
See docs/runbooks/dots-history-export.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import subprocess  # noqa: S404
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services.dots_history_export.leak_check import (
    MIN_FORBIDDEN_VALUE_CHARS,
    LeakCheckFailed,
)
from app.services.dots_history_export.projection import (
    RefHasher,
    SaltError,
    Window,
    project,
)
from app.services.dots_history_export.publish import (
    OutputDirectoryError,
    publish,
    resolve_output_directory,
)
from app.services.dots_history_export.reader import default_since, read_sources

REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_MAX_VIOLATIONS_PRINTED = 50
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LEAK = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export de-identified order and fill history for dots (read-only)"
    )
    parser.add_argument(
        "--database-url-env",
        required=True,
        metavar="NAME",
        help="Name of the environment variable that holds the PostgreSQL URL",
    )
    salt = parser.add_mutually_exclusive_group(required=True)
    salt.add_argument(
        "--salt-file",
        type=Path,
        help="Owner-only file outside the repository that holds the salt",
    )
    salt.add_argument(
        "--salt-env",
        metavar="NAME",
        help="Name of the environment variable that holds the salt",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="New or empty output directory outside the repository",
    )
    parser.add_argument(
        "--since", help="Window start, ISO date or datetime (default: until - 6 months)"
    )
    parser.add_argument(
        "--until", help="Exclusive window end, ISO date or datetime (default: now)"
    )
    parser.add_argument(
        "--deny-values-file",
        type=Path,
        help=(
            "File outside the repository with one value per line (for example "
            "account numbers) that must not appear in the output"
        ),
    )
    parser.add_argument(
        "--code-commit",
        help="Commit recorded in the manifest when git cannot answer (container)",
    )
    return parser


def parse_instant(raw: str, *, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f"{option} must be an ISO date or datetime") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def resolve_window(args: argparse.Namespace, *, now: datetime) -> Window:
    until = parse_instant(args.until, option="--until") if args.until else now
    since = (
        parse_instant(args.since, option="--since")
        if args.since
        else default_since(until)
    )
    if since >= until:
        raise ValueError("--since must be before --until")
    return Window(since=since, until=until)


def _env_value(name: str, *, option: str) -> str:
    if not _ENV_NAME.fullmatch(name):
        raise ValueError(f"{option} must be an environment variable name")
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"environment variable {name} is not set or empty")
    return value.strip()


def _database_url(name: str) -> str:
    raw = _env_value(name, option="--database-url-env")
    try:
        url = make_url(raw)
        if url.get_backend_name() != "postgresql" or not url.database:
            raise ValueError
        return url.render_as_string(hide_password=False)
    except Exception:
        # Parser messages can quote the URL; never chain or repeat them.
        raise ValueError(
            f"environment variable {name} does not hold a complete PostgreSQL URL"
        ) from None


def _outside_repo_file(path: Path, *, option: str, repo_root: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        raise ValueError(f"{option} must be outside the repository")
    if not resolved.is_file():
        raise ValueError(f"{option} is not a readable file")
    return resolved


def load_salt(args: argparse.Namespace, *, repo_root: Path) -> bytes:
    if args.salt_env:
        return _env_value(args.salt_env, option="--salt-env").encode("utf-8")
    path = _outside_repo_file(args.salt_file, option="--salt-file", repo_root=repo_root)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("--salt-file must be readable by its owner only (chmod 600)")
    return path.read_bytes().strip()


def load_deny_values(path: Path | None, *, repo_root: Path) -> frozenset[str]:
    if path is None:
        return frozenset()
    resolved = _outside_repo_file(
        path, option="--deny-values-file", repo_root=repo_root
    )
    lines = resolved.read_text("utf-8").splitlines()
    values = frozenset(line.strip() for line in lines if line.strip())
    # A shorter value would be ignored by the check, so it must not pass silently.
    short = sum(len(value) < MIN_FORBIDDEN_VALUE_CHARS for value in values)
    if short:
        raise ValueError(
            f"--deny-values-file has {short} value(s) shorter than "
            f"{MIN_FORBIDDEN_VALUE_CHARS} characters"
        )
    return values


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(REPO_ROOT), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def resolve_code_commit(explicit: str | None) -> tuple[str, bool | None]:
    if explicit:
        if not _COMMIT.fullmatch(explicit.strip()):
            raise ValueError("--code-commit must be a lowercase hexadecimal commit")
        return explicit.strip(), None
    head = _git("rev-parse", "HEAD")
    if head is None or not _COMMIT.fullmatch(head.strip()):
        raise ValueError("git cannot name the commit here; pass --code-commit")
    status = _git("status", "--porcelain")
    return head.strip(), None if status is None else bool(status.strip())


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Any | None = None,
    now: datetime | None = None,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    window = resolve_window(args, now=now or datetime.now(UTC))
    salt = load_salt(args, repo_root=repo_root)
    hasher = RefHasher(salt)
    deny_values = load_deny_values(args.deny_values_file, repo_root=repo_root)
    out_dir = resolve_output_directory(args.out, repo_root=repo_root)
    code_commit, code_dirty = resolve_code_commit(args.code_commit)

    engine = None
    if session_factory is None:
        engine = create_async_engine(
            _database_url(args.database_url_env), poolclass=NullPool
        )
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    try:
        async with session_factory() as session:
            try:
                sources = await read_sources(session, window)
            finally:
                await session.rollback()
    finally:
        if engine is not None:
            await engine.dispose()

    return publish(
        project(sources, hasher=hasher, window=window),
        out_dir=out_dir,
        window=window,
        salt=salt,
        salt_fingerprint=hasher.fingerprint(),
        code_commit=code_commit,
        code_dirty=code_dirty,
        extra_forbidden=deny_values,
    )


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "exported",
        "window": manifest["window"],
        "code_commit": manifest["code_commit"],
        "row_counts": {
            name: table["row_count"] for name, table in manifest["tables"].items()
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = asyncio.run(run(args))
    except LeakCheckFailed as exc:
        # Locations and rule names only; the offending value is never repeated.
        print(
            json.dumps(
                {
                    "error": "leak_check_failed",
                    "violation_count": len(exc.violations),
                    "violations": [
                        item.as_dict()
                        for item in exc.violations[:_MAX_VIOLATIONS_PRINTED]
                    ],
                }
            ),
            file=sys.stderr,
        )
        return EXIT_LEAK
    except (ValueError, SaltError, OutputDirectoryError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(
            json.dumps({"error": f"export_failed:{exc.__class__.__name__}"}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(json.dumps(_summary(manifest), ensure_ascii=False, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
