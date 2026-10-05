"""Dockerfile.api ships research/nautilus_scalping, and only that part of research/.

The H5 entry scripts (``scripts/binance_h5_*.py``: the demo runner, the read-only
truth gate and the heartbeat watcher) import
``research.nautilus_scalping.rob974_features`` through the H5 modules
(``demo_strategy_loop/strategy.py``, ``h5/strategy``, ``h5/state``, ``h5/client``).
While the final stage did not copy that package, every ``python -m
scripts.binance_h5_*`` in the deployed image ended with
``ModuleNotFoundError: No module named 'research'`` before argparse ran.

The image is simulated, not built. The final stage's COPY directives are applied to
a throwaway directory, honouring ``.dockerignore`` with Docker's root-relative
matching, and the result is checked two ways:

- statically, every first-party module the entry scripts reach (function-level
  imports too) must be in that file set;
- at runtime, a fresh interpreter whose ``sys.path`` has every repository source
  root removed imports each entry script from that tree alone.

Third-party packages come from the interpreter running the tests, standing in for
the image's ``/app/.venv``; they are not part of what is being decided here.

Every entry script that exists is covered (glob), so the truth gate and the
heartbeat watcher join the check by existing, not by a list edit.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import glob
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRST_PARTY = ("app", "research", "research_contracts", "scripts")
ENTRY_SCRIPTS = tuple(
    sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.glob("scripts/binance_h5_*.py")
    )
)
RESEARCH_MODULE = "research.nautilus_scalping.rob974_features"
SCRIPT_COPY = "COPY research/nautilus_scalping/ ./research/nautilus_scalping/"


# --- the Dockerfile final stage and .dockerignore ------------------------------


@dataclasses.dataclass(frozen=True)
class Copy:
    sources: tuple[str, ...]
    dest: str  # absolute path inside the image
    dest_is_dir: bool


def final_stage(dockerfile: str) -> tuple[list[Copy], str]:
    """The context-sourced COPYs of the last stage and its final WORKDIR.

    A ``--from`` copy takes files from another build stage, not the context, and is
    left out. The final WORKDIR is the runtime cwd, where ``python -m`` finds
    ``app``, ``scripts`` and ``research``.
    """
    final = re.split(r"^FROM ", dockerfile, flags=re.M)[-1]
    workdir, copies = "/", []
    for raw in re.sub(r"\\\n", " ", final).splitlines():
        head = raw.split(None, 1)[:1]
        if not head or head[0].upper() not in {"WORKDIR", "COPY"}:
            continue
        words = shlex.split(raw, comments=True)
        if words[0].upper() == "WORKDIR" and len(words) == 2:
            workdir = posixpath.normpath(posixpath.join(workdir, words[1]))
        elif words[0].upper() == "COPY" and not any(
            w.startswith("--from=") for w in words
        ):
            operands = [w for w in words[1:] if not w.startswith("--")]
            if len(operands) >= 2:
                copies.append(
                    Copy(
                        sources=tuple(operands[:-1]),
                        dest=posixpath.normpath(posixpath.join(workdir, operands[-1])),
                        dest_is_dir=operands[-1].endswith("/") or len(operands) > 2,
                    )
                )
    return copies, workdir


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end]
            out.append("[" + ("^" + body[1:] if body[0] in "!^" else body) + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def dockerignore(text: str) -> Callable[[str], bool]:
    """Docker's matching: patterns are relative to the context root, ``*`` stops at ``/``.

    So ``tests/`` or ``*.md`` ignore the root-level one only, and a pattern that
    matches a directory ignores everything under it. The last matching rule wins
    and ``!`` re-includes.
    """
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = posixpath.normpath(line[1:].strip() if negate else line).lstrip("/")
        rules.append((negate, _glob_regex(pattern)))

    def ignored(path: str) -> bool:
        parts = path.split("/")
        prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
        verdict = False
        for negate, rx in rules:
            if any(rx.match(prefix) for prefix in prefixes):
                verdict = not negate
        return verdict

    return ignored


class ImageBuildError(AssertionError):
    """The COPY would fail the real build."""


def image_files(
    dockerfile: str, ignore_text: str, repo: Path = REPO_ROOT
) -> tuple[dict[str, Path], str]:
    """{absolute image path: source file} for the final stage's COPYs, and the runtime cwd.

    Bytecode caches are left out: they are not source and a local checkout may or
    may not have them.
    """
    copies, workdir = final_stage(dockerfile)
    ignored = dockerignore(ignore_text)
    files: dict[str, Path] = {}
    for copy in copies:
        for source in copy.sources:
            matches = (
                sorted(glob.glob(source, root_dir=repo))
                if re.search(r"[*?\[]", source)
                else [source]
            )
            if not matches:
                raise ImageBuildError(f"COPY {source}: no match in the build context")
            for match in matches:
                rel = posixpath.normpath(match)
                path = repo / rel
                if ignored(rel) or not path.exists():
                    raise ImageBuildError(
                        f"COPY {source}: not in the build context (missing or .dockerignore)"
                    )
                if not path.is_dir():
                    dest = (
                        posixpath.join(copy.dest, posixpath.basename(rel))
                        if copy.dest_is_dir
                        else copy.dest
                    )
                    files[dest] = path
                    continue
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                    for name in sorted(filenames):
                        full = Path(dirpath, name)
                        if name.endswith(".pyc") or ignored(
                            full.relative_to(repo).as_posix()
                        ):
                            continue
                        files[
                            posixpath.join(copy.dest, full.relative_to(path).as_posix())
                        ] = full
    return files, workdir


def under_workdir(files: dict[str, Path], workdir: str) -> set[str]:
    prefix = workdir.rstrip("/") + "/"
    return {key[len(prefix) :] for key in files if key.startswith(prefix)}


# --- what the entry scripts reach -----------------------------------------------


def _module_file(name: str, repo: Path) -> Path | None:
    if name.split(".")[0] not in FIRST_PARTY:
        return None
    base = repo.joinpath(*name.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


@functools.cache
def reached_files(entries: tuple[str, ...], repo: Path = REPO_ROOT) -> frozenset[str]:
    """Repo-relative first-party module files every import reaches (lazy ones too).

    Importing ``a.b.c`` also executes the ``__init__`` of ``a`` and ``a.b``, so each
    prefix is followed.
    """
    seen: set[Path] = set()
    stack = [repo / entry for entry in entries]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level > 0:
                package = path.relative_to(repo).parent.parts
                base = package[: len(package) - (node.level - 1)]
                prefix = ".".join(
                    base + (tuple(node.module.split(".")) if node.module else ())
                )
                names = [prefix] + [f"{prefix}.{a.name}" for a in node.names]
            for name in names:
                parts = name.split(".")
                for end in range(1, len(parts) + 1):
                    target = _module_file(".".join(parts[:end]), repo)
                    if target is not None:
                        stack.append(target)
    return frozenset(p.relative_to(repo).as_posix() for p in seen)


def missing_from_image(files: dict[str, Path], workdir: str) -> list[str]:
    return sorted(reached_files(ENTRY_SCRIPTS) - under_workdir(files, workdir))


def research_scope_violations(files: dict[str, Path], workdir: str) -> list[str]:
    """Image files under research/ other than nautilus_scalping (and the package marker)."""
    return sorted(
        rel
        for rel in under_workdir(files, workdir)
        if rel.startswith("research/")
        and not rel.startswith("research/nautilus_scalping/")
        and rel != "research/__init__.py"
    )


# --- importing the entry scripts from the simulated image alone -------------------

_BOOTSTRAP = r"""
import importlib, json, os, sys, traceback

image = os.path.realpath(sys.argv[1])
first_party = set(sys.argv[2].split(","))
modules = sys.argv[3:]


def real(p):
    return os.path.realpath(p or os.getcwd())


def source_root(p):
    return os.path.isfile(os.path.join(p, "pyproject.toml")) and os.path.isdir(
        os.path.join(p, "app")
    )


# The test interpreter's editable install points at a checkout that has all of
# research/. Drop every source root but the image, so only the COPY set resolves.
sys.path[:] = [p for p in sys.path if real(p) == image or not source_root(real(p))]
if image not in [real(p) for p in sys.path]:
    sys.path.insert(0, image)

errors = {}
for name in modules:
    try:
        importlib.import_module(name)
    except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
        errors[name] = f"{type(exc).__name__}: {exc}"

leaks = []
for name, mod in list(sys.modules.items()):
    if name.split(".")[0] not in first_party:
        continue
    where = [mod.__file__] if getattr(mod, "__file__", None) else list(getattr(mod, "__path__", []))
    leaks += [f"{name}: {w}" for w in where if not real(w).startswith(image + os.sep)]

print(json.dumps({"errors": errors, "leaks": leaks, "loaded": sorted(sys.modules)}))
"""


# app.core.config.Settings has seven required fields and is built on import; the
# deployed container gets them from its env file. Inert placeholders, never a real
# value, and nothing here opens a connection: the engine is lazy.
_SETTINGS_PLACEHOLDERS = {
    "KIS_APP_KEY": "placeholder",
    "KIS_APP_SECRET": "placeholder",
    "OPENDART_API_KEY": "placeholder",
    "DATABASE_URL": "postgresql+asyncpg://placeholder:placeholder@127.0.0.1:1/placeholder",
    "UPBIT_ACCESS_KEY": "placeholder",
    "UPBIT_SECRET_KEY": "placeholder",
    "SECRET_KEY": "Inert_Placeholder_Key_0123456789_Not_Real",  # passes the strength check
}


@dataclasses.dataclass(frozen=True)
class RuntimeReport:
    errors: dict[str, str]
    leaks: list[str]
    loaded: frozenset[str]


_STRIP_SOURCE_ROOTS = "sys.path[:] = [p for p in sys.path if real(p) == image or not source_root(real(p))]"
assert _STRIP_SOURCE_ROOTS in _BOOTSTRAP


def run_bootstrap(
    cwd: Path,
    modules: list[str],
    *,
    bootstrap: str = _BOOTSTRAP,
    extra_env: dict[str, str] | None = None,
) -> RuntimeReport:
    """Import ``modules`` in a fresh interpreter started in ``cwd`` (the image's /app)."""
    proc = subprocess.run(
        [sys.executable, "-c", bootstrap, str(cwd), ",".join(FIRST_PARTY), *modules],
        cwd=cwd,
        # A bare environment: nothing of the test run's configuration or credentials
        # reaches the imports, and none of the output below is an environment dump.
        env={
            "HOME": str(cwd),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            **_SETTINGS_PLACEHOLDERS,
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, f"bootstrap failed: {proc.stderr[-2000:]}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    return RuntimeReport(report["errors"], report["leaks"], frozenset(report["loaded"]))


def import_from_image(
    files: dict[str, Path], workdir: str, root: Path
) -> RuntimeReport:
    """Materialize the COPY set under ``root`` and import every entry script there."""
    for key, source in files.items():
        target = root / key.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    modules = [
        Path(e).with_suffix("").as_posix().replace("/", ".") for e in ENTRY_SCRIPTS
    ]
    return run_bootstrap(root / workdir.lstrip("/"), modules)


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile.api").read_text()


@pytest.fixture(scope="module")
def ignore_text() -> str:
    return (REPO_ROOT / ".dockerignore").read_text()


@pytest.fixture(scope="module")
def real_image(dockerfile, ignore_text) -> tuple[dict[str, Path], str]:
    return image_files(dockerfile, ignore_text)


@pytest.fixture(scope="module")
def real_runtime(real_image, tmp_path_factory) -> RuntimeReport:
    files, workdir = real_image
    return import_from_image(files, workdir, tmp_path_factory.mktemp("image"))


# --- the real Dockerfile --------------------------------------------------------


def test_entry_scripts_include_the_runner() -> None:
    # The glob must not be vacuous. Once they exist, the truth gate and the heartbeat
    # watcher are picked up by the same glob.
    assert "scripts/binance_h5_demo.py" in ENTRY_SCRIPTS


def test_the_scripts_do_reach_research() -> None:
    # The premise of the COPY: without it this test would pin nothing.
    reached = reached_files(ENTRY_SCRIPTS)
    assert "research/nautilus_scalping/rob974_features.py" in reached


def test_image_ships_every_first_party_module_the_entry_scripts_reach(
    real_image,
) -> None:
    files, workdir = real_image
    assert missing_from_image(files, workdir) == []


def test_entry_scripts_import_from_the_image_file_set_alone(real_runtime) -> None:
    assert real_runtime.errors == {}
    assert real_runtime.leaks == []
    # the import really went through the H5 modules into the shipped package
    assert RESEARCH_MODULE in real_runtime.loaded


def test_only_nautilus_scalping_is_added_from_research(real_image) -> None:
    files, workdir = real_image
    shipped = {
        rel for rel in under_workdir(files, workdir) if rel.startswith("research/")
    }
    assert shipped, "research/nautilus_scalping must ship"
    assert research_scope_violations(files, workdir) == []
    assert "research/nautilus_scalping/rob974_features.py" in shipped
    # one COPY from research/, exactly this one
    dockerfile_copies, _ = final_stage((REPO_ROOT / "Dockerfile.api").read_text())
    research_copies = [
        (c.sources, c.dest)
        for c in dockerfile_copies
        if any(s.split("/")[0] == "research" for s in c.sources)
    ]
    assert research_copies == [
        (("research/nautilus_scalping/",), "/app/research/nautilus_scalping")
    ]


# --- the check catches what it exists to catch (mutants of the Dockerfile) -----


def _without(dockerfile: str, line: str) -> str:
    assert line in dockerfile
    return dockerfile.replace(line + "\n", "")


def test_removing_the_copy_line_fails_both_layers(
    dockerfile, ignore_text, tmp_path
) -> None:
    files, workdir = image_files(_without(dockerfile, SCRIPT_COPY), ignore_text)
    assert "research/nautilus_scalping/rob974_features.py" in missing_from_image(
        files, workdir
    )
    runtime = import_from_image(files, workdir, tmp_path)
    assert runtime.errors, "an import must fail when research is not copied"
    assert any("No module named 'research'" in e for e in runtime.errors.values())
    assert RESEARCH_MODULE not in runtime.loaded


def test_copying_a_different_research_package_does_not_satisfy_the_scripts(
    dockerfile, ignore_text
) -> None:
    mutant = dockerfile.replace(
        SCRIPT_COPY, "COPY research/kr_corpus/ ./research/kr_corpus/"
    )
    files, workdir = image_files(mutant, ignore_text)
    assert "research/nautilus_scalping/rob974_features.py" in missing_from_image(
        files, workdir
    )


def test_a_copy_that_lands_outside_the_runtime_directory_does_not_count(
    dockerfile, ignore_text
) -> None:
    mutant = dockerfile.replace(
        SCRIPT_COPY, "COPY research/nautilus_scalping/ /srv/research/nautilus_scalping/"
    )
    files, workdir = image_files(mutant, ignore_text)
    assert "research/nautilus_scalping/rob974_features.py" in missing_from_image(
        files, workdir
    )


def test_a_copy_in_an_earlier_stage_does_not_count(dockerfile, ignore_text) -> None:
    head, tail = dockerfile.split("FROM python:3.13-slim AS final")
    mutant = (
        head.replace("WORKDIR /app\n", "WORKDIR /app\n" + SCRIPT_COPY + "\n", 1)
        + "FROM python:3.13-slim AS final"
        + _without(tail, SCRIPT_COPY)
    )
    assert SCRIPT_COPY in mutant
    files, workdir = image_files(mutant, ignore_text)
    assert "research/nautilus_scalping/rob974_features.py" in missing_from_image(
        files, workdir
    )


def test_a_from_copy_does_not_count_as_context_files(dockerfile, ignore_text) -> None:
    mutant = dockerfile.replace(
        SCRIPT_COPY,
        "COPY --from=builder research/nautilus_scalping/ ./research/nautilus_scalping/",
    )
    files, workdir = image_files(mutant, ignore_text)
    assert "research/nautilus_scalping/rob974_features.py" in missing_from_image(
        files, workdir
    )


def test_dockerignoring_research_breaks_the_copy(dockerfile, ignore_text) -> None:
    with pytest.raises(ImageBuildError):
        image_files(dockerfile, ignore_text + "\nresearch/\n")


def test_copying_all_of_research_fails_the_scope_pin(dockerfile, ignore_text) -> None:
    mutant = dockerfile.replace(SCRIPT_COPY, "COPY research/ ./research/")
    files, workdir = image_files(mutant, ignore_text)
    assert missing_from_image(files, workdir) == []  # the scripts would run...
    violations = research_scope_violations(files, workdir)
    assert any(
        v.startswith("research/kr_corpus/") for v in violations
    )  # ...but too much ships


def test_a_second_research_copy_fails_the_scope_pin(dockerfile, ignore_text) -> None:
    mutant = dockerfile.replace(
        SCRIPT_COPY, SCRIPT_COPY + "\nCOPY research/kr_corpus/ ./research/kr_corpus/"
    )
    files, workdir = image_files(mutant, ignore_text)
    assert research_scope_violations(files, workdir)


def test_the_package_marker_is_the_only_extra_research_file_allowed(real_image) -> None:
    files, workdir = real_image
    with_marker = {
        **files,
        f"{workdir}/research/__init__.py": REPO_ROOT / "Dockerfile.api",
    }
    assert research_scope_violations(with_marker, workdir) == []
    stray = {**files, f"{workdir}/research/other.py": REPO_ROOT / "Dockerfile.api"}
    assert research_scope_violations(stray, workdir) == ["research/other.py"]


def test_the_harness_resolves_first_party_code_from_the_image_alone(tmp_path) -> None:
    # A one-file "image" whose script imports a research module it does not ship, while
    # the checkout (which has it) is put on PYTHONPATH on purpose.
    image = tmp_path / "app"
    (image / "scripts").mkdir(parents=True)
    (image / "scripts" / "__init__.py").write_text("")
    (image / "scripts" / "probe.py").write_text(f"import {RESEARCH_MODULE}\n")
    checkout = {"PYTHONPATH": str(REPO_ROOT)}

    stripped = run_bootstrap(image, ["scripts.probe"], extra_env=checkout)
    assert "No module named 'research'" in stripped.errors["scripts.probe"]
    assert stripped.leaks == []

    # Without the source-root strip the same import succeeds out of the checkout: that
    # is what the strip prevents, and what the leak guard reports if it ever fails.
    unstripped = run_bootstrap(
        image,
        ["scripts.probe"],
        bootstrap=_BOOTSTRAP.replace(_STRIP_SOURCE_ROOTS, "pass"),
        extra_env=checkout,
    )
    assert unstripped.errors == {}
    assert any(
        leak.startswith(f"{RESEARCH_MODULE}: {REPO_ROOT}") for leak in unstripped.leaks
    )


# --- the simulation itself ------------------------------------------------------


def test_dockerignore_patterns_are_root_relative() -> None:
    ignored = dockerignore("tests/\n*.md\n!README.md\n**/*.key\n.env.*\n")
    assert ignored("tests/test_x.py")
    assert not ignored("research/nautilus_scalping/tests/test_x.py")  # root-level only
    assert ignored("CHANGELOG.md") and not ignored("README.md")
    assert not ignored("docs/notes.md")  # * does not cross /
    assert ignored("a/b/c.key")  # ** does
    assert ignored(".env.prod") and not ignored("app/.env.prod")


def test_final_stage_tracks_workdir_and_skips_other_stages() -> None:
    text = (
        "FROM a AS builder\nWORKDIR /build\nCOPY x/ ./x/\n"
        "FROM b AS final\nWORKDIR /srv\nWORKDIR app\nCOPY y/ ./y/\n"
        "COPY --from=builder /build/x /srv/app/x\nCOPY f.py g.py ./\nCOPY z.py /abs/z.py\n"
    )
    copies, workdir = final_stage(text)
    assert workdir == "/srv/app"
    assert [(c.sources, c.dest, c.dest_is_dir) for c in copies] == [
        (("y/",), "/srv/app/y", True),
        (("f.py", "g.py"), "/srv/app", True),
        (("z.py",), "/abs/z.py", False),
    ]


def test_the_real_dockerfile_final_stage_runs_from_app(real_image) -> None:
    _, workdir = real_image
    assert workdir == "/app"
