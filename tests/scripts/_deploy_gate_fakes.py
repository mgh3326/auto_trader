"""Offline registry/Actions fakes used with the real exact-SHA deployment gate.

Python stateful Docker fakes run in one cached helper per pytest process. Shell
fakes keep the existing front end and executable behavior.
"""

from __future__ import annotations

import atexit
import builtins
import hashlib
import io
import json
import os
import shlex
import shutil
import socketserver
import stat
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE_HELPER = REPO / "scripts" / "deploy_ci_gate.py"
GATE_SHA = "a" * 40

_FRONT_DOCKER = """\
#!/bin/sh
case "${1-} ${2-} ${3-}" in
  'buildx imagetools inspect')
    printf '%s\\n' "$*" >> __LOG__
    for arg do format="$arg"; done
    case "$format" in
      *'.Manifest'*) printf '%s\\n' __MANIFEST__ ;;
      *'.Image'*) printf '%s\\n' __CONFIG__ ;;
      *) exit 42 ;;
    esac
    ;;
  *) exec __BACKEND__ "$@" ;;
esac
"""

_FAKE_GH = """\
#!/bin/sh
printf '%s\\n' "$*" >> __LOG__
[ "$#" -eq 6 ] && [ "$1 $2 $3 $4 $5" = 'api --method GET --paginate --slurp' ] || exit 42
case "$6" in
  *'/actions/workflows/test.yml/runs?'*) printf '%s\\n' __RUNS__ ;;
  *'/actions/runs/100/attempts/1/jobs?'*) printf '%s\\n' __JOBS__ ;;
  *) exit 42 ;;
esac
"""

_FAKE_ENV_NAMES = (
    "FAKE_DOCKER_STATE",
    "FAKE_DOCKER_LOG",
    "FAKE_NEW_DIGEST",
    "FAKE_OLD_DIGEST",
    "FAKE_NEW_ID",
    "FAKE_FAULTS",
    "FAKE_FAIL_NAME",
    "FAKE_FAIL_HEALTH_NAME",
    "FAKE_FAIL_HEALTH",
    "FAKE_FAIL_RUN_NAME",
    "FAKE_MISMATCH_NAME",
    "FAKE_SUCCESS_MISMATCH_NAME",
    "FAKE_STOPPED_NAME",
    "FAKE_STOPPED_AFTER_PROMOTION_NAME",
    "FAKE_STOP_AFTER_PULL_NAME",
    "FAKE_START_AFTER_PULL_NAME",
    "FAKE_UNRESOLVED_NAME",
    "FAKE_TRANSIENT_INSPECT_NAME",
    "FAKE_FAIL_HAPROXY_HUP_ONCE",
)
_SERVER = None
_SERVER_THREAD = None
_SERVER_DIRECTORY: Path | None = None
_SERVER_LOCK = threading.Lock()
_COMPILED_BACKENDS: dict[str, types.CodeType] = {}


def _serve_python_docker_fakes() -> str | None:
    """Start one in-process backend for Python fake Docker scripts, if nc exists."""
    global _SERVER, _SERVER_THREAD, _SERVER_DIRECTORY
    if shutil.which("nc") is None:
        return None
    with _SERVER_LOCK:
        if _SERVER is not None:
            return str(_SERVER_DIRECTORY / "docker.sock")

        directory = Path(tempfile.mkdtemp(prefix="deploy-fake-docker-"))
        socket_path = directory / "docker.sock"

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                request = bytearray()
                end_marker = b"--END--\0"
                while not request.endswith(end_marker):
                    chunk = self.rfile.read1(4096)
                    if not chunk:
                        raise ConnectionError(
                            "fake Docker request ended before its frame"
                        )
                    request.extend(chunk)
                fields = bytes(request[: -len(end_marker)]).split(b"\0")
                if fields and fields[-1] == b"":
                    fields.pop()
                backend, gate_log, manifest, config, stderr_path, status_path = (
                    value.decode() for value in fields[:6]
                )
                cursor = 6
                env: dict[str, str] = {}
                while fields[cursor] != b"--ARGS--":
                    key, value = fields[cursor].decode(), fields[cursor + 1].decode()
                    env[key] = value
                    cursor += 2
                args = [value.decode() for value in fields[cursor + 1 :]]
                output = ""
                error = ""
                status = 0
                if args[:3] == ["buildx", "imagetools", "inspect"]:
                    with open(gate_log, "a") as log:
                        log.write(" ".join(args) + "\n")
                    fmt = args[-1]
                    if ".Manifest" in fmt:
                        output = manifest + "\n"
                    elif ".Image" in fmt:
                        output = config + "\n"
                    else:
                        status = 42
                else:
                    source = Path(backend).read_text()
                    source_lines = source.splitlines()[1:]
                    for index, line in enumerate(source_lines):
                        if line.startswith("import "):
                            imports = [name.strip() for name in line[7:].split(",")]
                            kept = [
                                name for name in imports if name not in {"os", "sys"}
                            ]
                            source_lines[index] = (
                                "import " + ", ".join(kept) if kept else ""
                            )
                    body = "\n".join(source_lines)
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    code = _COMPILED_BACKENDS.get(digest)
                    if code is None:
                        code = compile(body, backend, "exec")
                        _COMPILED_BACKENDS[digest] = code
                    stdout, stderr = io.StringIO(), io.StringIO()
                    fake_sys = types.SimpleNamespace(
                        argv=[backend, *args], stdout=stdout, stderr=stderr
                    )

                    def fake_exit(code: object = 0) -> None:
                        raise SystemExit(code)

                    fake_sys.exit = fake_exit

                    def fake_print(*values: object, **kwargs: object) -> None:
                        target = kwargs.get("file")
                        stream = stderr if target is stderr else stdout
                        kwargs.pop("file", None)
                        builtins.print(*values, file=stream, **kwargs)

                    namespace = {
                        "__name__": "__main__",
                        "__file__": backend,
                        "os": types.SimpleNamespace(environ=env, path=os.path),
                        "sys": fake_sys,
                        "print": fake_print,
                    }
                    try:
                        try:
                            exec(code, namespace)
                        except SystemExit as exc:
                            status = exc.code if isinstance(exc.code, int) else 0
                    except BaseException as exc:
                        error = f"{type(exc).__name__}: {exc}\n"
                        status = 1
                    output, error = stdout.getvalue(), error or stderr.getvalue()
                Path(stderr_path).write_text(error)
                Path(status_path).write_text(str(status))
                self.wfile.write(output.encode())

        class Server(socketserver.UnixStreamServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(str(socket_path), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _SERVER, _SERVER_THREAD, _SERVER_DIRECTORY = server, thread, directory

        def cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join()
            shutil.rmtree(directory, ignore_errors=True)

        atexit.register(cleanup)
        return str(socket_path)


def _python_docker_client(
    socket_path: str, backend: Path, bindir: Path, digest: str
) -> str:
    manifest = json.dumps({"digest": digest.split("@", 1)[1]})
    config = json.dumps(
        {"config": {"Labels": {"org.opencontainers.image.revision": GATE_SHA}}}
    )
    env_list = " ".join(_FAKE_ENV_NAMES)
    return f"""#!/bin/sh
tmp={shlex.quote(str(Path(socket_path).parent))}/response.$$
err="$tmp.err"
status="$tmp.status"
{{
  printf '%s\\0' {shlex.quote(str(backend))} {shlex.quote(str(bindir / "gate-calls.log"))} {shlex.quote(manifest)} {shlex.quote(config)} "$err" "$status"
  for key in {env_list}; do
    eval "value=\\${{${{key}}-}}"
    printf '%s\\0%s\\0' "$key" "$value"
  done
  printf '%s\\0' --ARGS-- "$@" --END--
}} | {shlex.quote(shutil.which("nc") or "nc")} -U {shlex.quote(socket_path)}
IFS= read -r code < "$status"
if [ -s "$err" ]; then cat "$err" >&2; fi
rm -f "$err" "$status"
exit "$code"
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_gate_fakes(bindir: Path, docker_script: str, digest: str) -> None:
    """Serve gate evidence, preserving the original stateful Docker fake."""
    backend = bindir / "_docker_backend"
    _write_exec(backend, docker_script)
    log = shlex.quote(str(bindir / "gate-calls.log"))
    is_python = (
        docker_script.startswith("#!") and "python" in docker_script.splitlines()[0]
    )
    socket_path = _serve_python_docker_fakes() if is_python else None
    if socket_path is not None:
        client = _python_docker_client(socket_path, backend, bindir, digest)
        _write_exec(bindir / "docker", client)
    else:
        manifest = json.dumps({"digest": digest.split("@", 1)[1]})
        config = json.dumps(
            {"config": {"Labels": {"org.opencontainers.image.revision": GATE_SHA}}}
        )
        front = (
            _FRONT_DOCKER.replace("__LOG__", log)
            .replace("__MANIFEST__", shlex.quote(manifest))
            .replace("__CONFIG__", shlex.quote(config))
            .replace("__BACKEND__", shlex.quote(str(backend)))
        )
        _write_exec(bindir / "docker", front)
    runs = [{"id": 100, "run_attempt": 1, "event": "push", "head_sha": GATE_SHA}]
    names = ["lint", "taskiq-smoke", *(f"test (3.13, {n})" for n in range(1, 5))]
    jobs = [
        {"id": n, "name": name, "status": "completed", "conclusion": "success"}
        for n, name in enumerate(names, 1)
    ]
    gh = (
        _FAKE_GH.replace("__LOG__", log)
        .replace("__RUNS__", shlex.quote(json.dumps([{"workflow_runs": runs}])))
        .replace("__JOBS__", shlex.quote(json.dumps([{"jobs": jobs}])))
    )
    _write_exec(bindir / "gh", gh)


def install_gate_helper(script_directory: Path) -> Path:
    """Copy the standalone helper beside an installed/mutated deploy script."""
    target = script_directory / GATE_HELPER.name
    shutil.copy2(GATE_HELPER, target)
    return target
