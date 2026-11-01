"""Write-once file helpers and the public HTTP fetcher.

Every artifact in this package is written once: staged as ``.partial``,
fsynced, then published with ``os.link`` (fails if the target exists) so no
code path can overwrite a finished file. Logs are opened in append mode only.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

USER_AGENT = "auto_trader-research/ROB-1431 (public symbol directory archive reader)"

Fetcher = Callable[[str], bytes]


class AlreadyExistsError(FileExistsError):
    pass


def write_once(path: Path, data: bytes) -> None:
    """Publish ``data`` at ``path``; raise AlreadyExistsError if it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with open(partial, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.link(partial, path)
    except FileExistsError as exc:
        raise AlreadyExistsError(str(path)) from exc
    finally:
        partial.unlink(missing_ok=True)
    # Persist the new directory entry, not just the file contents.
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Block until this process holds an exclusive ``flock`` on ``path``.

    The lock file is created once and never removed; the kernel releases the
    lock when the descriptor closes, including when the process dies.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "ab") as fh:
        # A crash can leave a torn last line. Terminate it (it stays in the
        # log, ignored by ``read_jsonl``) instead of gluing our record to it.
        if fh.tell() > 0:
            with open(path, "rb") as rd:
                rd.seek(-1, os.SEEK_END)
                if rd.read(1) != b"\n":
                    fh.write(b"\n")
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[dict]:
    """Complete records only: a line is complete when it ends with a newline
    and parses. Torn lines from a crashed append are skipped, never repaired."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text("utf-8").split("\n")[:-1]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def code_commit(repo_root: Path) -> str:
    """HEAD SHA, suffixed ``-dirty`` when a *tracked* file has changes.

    Untracked files are ignored on purpose (``-uno``): build outputs written
    inside the repo must not mark their own generating commit dirty. Source
    used by a build is tracked by construction.
    """
    try:
        sha = subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(repo_root), "status", "--porcelain", "-uno"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https:// URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code, f"refusing non-https redirect to {newurl!r}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_fetcher(
    *,
    timeout: float = 60.0,
    min_interval: float = 1.0,
    max_requests: int = 600,
    retries: int = 3,
) -> Fetcher:
    """Polite GET for public, unauthenticated URLs (no credentials, ever).

    Spaces requests by ``min_interval`` seconds, retries 429/5xx with backoff,
    and refuses to exceed ``max_requests`` in one process.
    """
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx), _HttpsOnlyRedirects()
    )
    state = {"count": 0, "last": 0.0}

    def fetch(url: str) -> bytes:
        if not url.startswith("https://"):
            raise ValueError(f"refusing non-https URL {url!r}")
        for attempt in range(retries + 1):
            if state["count"] >= max_requests:
                raise RuntimeError(f"request budget {max_requests} exhausted")
            wait = state["last"] + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            state["count"] += 1
            state["last"] = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
            try:
                with opener.open(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == retries:
                    raise
            time.sleep(5 * 3**attempt)
        raise AssertionError("unreachable")

    return fetch
