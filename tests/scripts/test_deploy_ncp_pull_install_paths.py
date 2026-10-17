"""ROB-1380: deploy-ncp-pull.sh inputs on the NCP install layout.

Deploy #1 on 2026-10-16 ran the operator copy /root/at-run/deploy-ncp-pull.sh.
The HAProxy template was derived from ``$(dirname $0)/..`` and so pointed at
/root/ops/..., which does not exist; render_haproxy returned 78 with no message
after the API color had already been replaced. These tests run the script
from a copy outside the repository, as the host does, with fake docker/curl.

Deploy #4 on 2026-10-17 (operator-desk, hk 1605) measured the host layout:
the env files are /root/at-secrets/.env.api and /root/at-secrets/.env.scheduler,
and the template is /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl. The
``_ncp_root`` cases rebuild that tree under a fake root.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from tests.scripts.test_deploy_ncp_pull_rollback import (
    DEPLOY,
    INITIAL,
    KIS_OLD,
    NEW,
    OLD,
    _run,
)

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "ops/ncp/haproxy/haproxy.cfg.tmpl"
RUNBOOK = REPO / "docs/runbooks/ncp-pull-deploy.md"
MUTATING = {"pull", "run", "rm", "stop", "rename", "kill"}
# An empty value means unset for the script (`${MCP_HAPROXY_TEMPLATE:-}`), so
# the harness's repository template does not mask discovery.
NO_ENV_TEMPLATE = {"MCP_HAPROXY_TEMPLATE": ""}


def _install(tmp_path: Path, *, with_template: bool) -> Path:
    """Copy the script the way the runbook installs it (/root/at-run)."""
    install = tmp_path / "at-run-install"
    install.mkdir()
    script = install / "deploy-ncp-pull.sh"
    shutil.copy2(DEPLOY, script)
    script.chmod(0o750)
    if with_template:
        shutil.copy2(TEMPLATE, install / "haproxy.cfg.tmpl")
    return script


def _expected(name: str) -> str:
    return KIS_OLD if name == "at-kis-ws" else OLD


def _assert_untouched(calls: list[list[str]], state: dict[str, str], run_dir: Path):
    assert [c for c in calls if c[0] in MUTATING] == []
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit
    assert (run_dir / "api-active-color").read_text() == "blue\n"
    assert (run_dir / "deployed-digest").read_text() == OLD + "\n"
    assert not (run_dir / "haproxy.cfg").exists()


def _rendered_matches_template(run_dir: Path, template: Path) -> None:
    # Promotion moves API blue (8001) -> green (8002) and MCP blue -> green (8767).
    want = (
        template.read_text()
        .replace("__API_ACTIVE_PORT__", "8002")
        .replace("__MCP_ACTIVE_PORT__", "8767")
    )
    assert (run_dir / "haproxy.cfg").read_text() == want


def _token_lines() -> str:
    (tokens,) = re.findall(
        r"^declare -a MCP_TOKENS=\((.*)\)$", DEPLOY.read_text(), re.M
    )
    return "".join(f"{name}=x\n" for name in ["MCP_AUTH_TOKEN", *tokens.split()])


def _env_files(call: list[str]) -> list[str]:
    return [v for f, v in zip(call, call[1:], strict=False) if f == "--env-file"]


# --- (a) HAProxy template on the installed copy ------------------------------


def test_installed_copy_uses_the_template_installed_next_to_it(tmp_path: Path):
    script = _install(tmp_path, with_template=True)
    result, _, state, run_dir = _run(tmp_path, deploy=script, extra_env=NO_ENV_TEMPLATE)
    assert result.returncode == 0, result.stderr
    installed = script.parent / "haproxy.cfg.tmpl"
    assert f"haproxy template (installed): {installed}\n" in result.stdout
    _rendered_matches_template(run_dir, installed)
    assert state["at-api-green"] == NEW
    assert (run_dir / "deployed-digest").read_text() == NEW + "\n"


def test_installed_copy_without_template_fails_loudly_before_any_mutation(
    tmp_path: Path,
):
    """The deploy #1 shape: no template beside the copy, no ../ops tree."""
    script = _install(tmp_path, with_template=False)
    result, calls, state, run_dir = _run(
        tmp_path, deploy=script, extra_env=NO_ENV_TEMPLATE
    )
    assert result.returncode == 78
    assert "HAProxy template is unavailable" in result.stderr
    assert str(script.parent / "haproxy.cfg.tmpl") in result.stderr
    assert str(run_dir / "ops/ncp/haproxy/haproxy.cfg.tmpl") in result.stderr
    assert str(tmp_path / "ops/ncp/haproxy/haproxy.cfg.tmpl") in result.stderr
    assert "--haproxy-template" in result.stderr
    _assert_untouched(calls, state, run_dir)


def test_manual_rollback_also_resolves_the_template_before_mutation(
    tmp_path: Path,
):
    script = _install(tmp_path, with_template=False)
    result, calls, state, run_dir = _run(
        tmp_path, args=("--rollback",), deploy=script, extra_env=NO_ENV_TEMPLATE
    )
    assert result.returncode == 78
    assert "HAProxy template is unavailable" in result.stderr
    _assert_untouched(calls, state, run_dir)


def test_installed_and_checkout_templates_together_are_refused(tmp_path: Path):
    script = _install(tmp_path, with_template=True)
    checkout = tmp_path / "ops/ncp/haproxy/haproxy.cfg.tmpl"
    checkout.parent.mkdir(parents=True)
    shutil.copy2(TEMPLATE, checkout)
    result, calls, state, run_dir = _run(
        tmp_path, deploy=script, extra_env=NO_ENV_TEMPLATE
    )
    assert result.returncode == 78
    assert "HAProxy template is ambiguous" in result.stderr
    assert str(script.parent / "haproxy.cfg.tmpl") in result.stderr
    assert str(checkout) in result.stderr
    _assert_untouched(calls, state, run_dir)


def test_repository_checkout_still_finds_its_own_template(tmp_path: Path):
    result, _, _, run_dir = _run(tmp_path, extra_env=NO_ENV_TEMPLATE)
    assert result.returncode == 0, result.stderr
    assert f"haproxy template (checkout): {TEMPLATE}\n" in result.stdout
    _rendered_matches_template(run_dir, TEMPLATE)


def test_flag_selects_the_template_in_both_spellings(tmp_path: Path):
    for i, spelling in enumerate(("separate", "joined")):
        case = tmp_path / spelling
        case.mkdir()
        script = _install(case, with_template=False)
        chosen = case / "elsewhere" / "haproxy.cfg.tmpl"
        chosen.parent.mkdir()
        shutil.copy2(TEMPLATE, chosen)
        args = (
            ("--haproxy-template", str(chosen))
            if i == 0
            else (f"--haproxy-template={chosen}",)
        )
        result, _, _, run_dir = _run(
            case, args=args, deploy=script, extra_env=NO_ENV_TEMPLATE
        )
        assert result.returncode == 0, (spelling, result.stderr)
        assert f"haproxy template (--haproxy-template): {chosen}\n" in result.stdout
        _rendered_matches_template(run_dir, chosen)


def test_flag_wins_over_the_environment_variable(tmp_path: Path):
    script = _install(tmp_path, with_template=False)
    chosen = tmp_path / "chosen.tmpl"
    shutil.copy2(TEMPLATE, chosen)
    result, _, _, _ = _run(
        tmp_path,
        args=("--haproxy-template", str(chosen)),
        deploy=script,
        extra_env={"MCP_HAPROXY_TEMPLATE": str(tmp_path / "absent.tmpl")},
    )
    assert result.returncode == 0, result.stderr
    assert f"haproxy template (--haproxy-template): {chosen}\n" in result.stdout


def test_missing_explicit_template_never_falls_back_to_a_discovered_one(
    tmp_path: Path,
):
    script = _install(tmp_path, with_template=True)
    missing = tmp_path / "typo" / "haproxy.cfg.tmpl"
    for args, env, source in (
        (("--haproxy-template", str(missing)), NO_ENV_TEMPLATE, "--haproxy-template"),
        ((), {"MCP_HAPROXY_TEMPLATE": str(missing)}, "MCP_HAPROXY_TEMPLATE"),
    ):
        case = tmp_path / source.strip("-")
        case.mkdir()
        result, calls, state, run_dir = _run(
            case, args=args, deploy=script, extra_env=env
        )
        assert result.returncode == 78, source
        assert f"{missing} (from {source}) is not a readable file" in result.stderr
        _assert_untouched(calls, state, run_dir)


def test_flag_without_a_path_is_a_usage_error(tmp_path: Path):
    for args in (("--haproxy-template",), ("--haproxy-template=",)):
        case = tmp_path / str(len(args[0]))
        case.mkdir()
        result, calls, _, _ = _run(case, args=args)
        assert result.returncode == 64, args
        assert "--haproxy-template PATH" in result.stderr
        assert [c for c in calls if c[0] in MUTATING] == []


def test_flag_never_takes_the_next_option_as_its_path(tmp_path: Path):
    """A missing value must not swallow --dry-run/--rollback/--skip-kis-ws.

    Swallowed, --dry-run is lost: with a file of that name in the working
    directory the run would resolve it as the template and deploy.
    """
    for option in ("--dry-run", "--rollback", "--skip-kis-ws"):
        for args in (
            ("--haproxy-template", option),
            (f"--haproxy-template={option}",),
        ):
            case = tmp_path / f"{option.strip('-')}-{len(args)}"
            case.mkdir()
            result, calls, state, run_dir = _run(case, args=args)
            assert result.returncode == 64, args
            assert "--haproxy-template PATH" in result.stderr, args
            _assert_untouched(calls, state, run_dir)


def test_render_names_the_template_if_it_disappears_after_preflight() -> None:
    body = DEPLOY.read_text()
    (render,) = re.findall(r"^render_haproxy\(\) \{\n(.*?)^\}", body, re.M | re.S)
    assert "HAProxy template is unavailable at render" in render
    assert '[[ -f "$HAPROXY_TEMPLATE" ]] || return 78' not in render


# --- (b) env file defaults are the NCP names ---------------------------------


def test_default_env_files_are_the_ncp_host_names_in_order(tmp_path: Path):
    result, calls, _, run_dir = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    want = [str(run_dir / ".env.api"), str(run_dir / ".env.scheduler")]
    app_runs = [c for c in calls if c[0] == "run" and any(a.startswith(NEW) for a in c)]
    assert len(app_runs) >= len(INITIAL)
    for call in app_runs:
        assert _env_files(call) == want, call
    assert f"env file 1 (AT_RUNTIME_ENV_FILE): {want[0]}\n" in result.stdout
    assert f"env file 2 (AT_SECRETS_ENV_FILE): {want[1]}\n" in result.stdout


def test_old_default_names_alone_fail_before_mutation_naming_the_override(
    tmp_path: Path,
):
    """A secrets directory holding only .env.runtime/.env.secrets is refused."""
    legacy = tmp_path / "legacy-secrets"
    legacy.mkdir()
    (legacy / ".env.runtime").write_text("x=y\n")
    (legacy / ".env.secrets").write_text(_token_lines())
    result, calls, state, run_dir = _run(
        tmp_path, extra_env={"AT_SECRETS_DIRECTORY": str(legacy)}
    )
    assert result.returncode == 78
    assert (
        f"required env file is unavailable: {legacy / '.env.api'} "
        "(set AT_RUNTIME_ENV_FILE to the host file)"
    ) in result.stderr
    assert [c for c in calls if c[0] in MUTATING] == []
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit
    assert (run_dir / "deployed-digest").read_text() == OLD + "\n"


def test_missing_second_default_names_its_override(tmp_path: Path):
    case_run = tmp_path / "only-api"
    case_run.mkdir()
    (case_run / ".env.api").write_text("x=y\n")
    result, calls, _, _ = _run(
        tmp_path, extra_env={"AT_SECRETS_DIRECTORY": str(case_run)}
    )
    assert result.returncode == 78
    assert (
        f"required env file is unavailable: {case_run / '.env.scheduler'} "
        "(set AT_SECRETS_ENV_FILE to the host file)"
    ) in result.stderr
    assert [c for c in calls if c[0] in MUTATING] == []


def test_explicit_env_file_overrides_are_still_honoured(tmp_path: Path):
    elsewhere = tmp_path / "secrets-dir"
    elsewhere.mkdir()
    api = elsewhere / "runtime.env"
    scheduler = elsewhere / "second.env"
    api.write_text("x=y\n")
    scheduler.write_text(_token_lines())
    result, calls, _, _ = _run(
        tmp_path,
        extra_env={
            "AT_RUNTIME_ENV_FILE": str(api),
            "AT_SECRETS_ENV_FILE": str(scheduler),
        },
    )
    assert result.returncode == 0, result.stderr
    app_runs = [c for c in calls if c[0] == "run" and any(a.startswith(NEW) for a in c)]
    assert app_runs
    for call in app_runs:
        assert _env_files(call) == [str(api), str(scheduler)], call


def test_script_defaults_are_literally_the_ncp_names() -> None:
    body = DEPLOY.read_text()
    assert (
        'readonly SECRETS_DIRECTORY="${AT_SECRETS_DIRECTORY:-/root/at-secrets}"' in body
    )
    assert (
        'readonly RUNTIME_ENV_FILE="${AT_RUNTIME_ENV_FILE:-${SECRETS_DIRECTORY}/.env.api}"'
        in body
    )
    assert (
        'readonly SECRETS_ENV_FILE="${AT_SECRETS_ENV_FILE:-${SECRETS_DIRECTORY}/.env.scheduler}"'
        in body
    )
    assert (
        'readonly HAPROXY_TEMPLATE_RUN_OPS="${RUN_DIRECTORY}/ops/ncp/haproxy/haproxy.cfg.tmpl"'
        in body
    )
    assert ".env.runtime" not in body and ".env.secrets" not in body
    # The #2219 defaults pointed into the run directory; the host keeps no
    # env files there.
    assert "${RUN_DIRECTORY}/.env." not in body


def test_unset_secrets_directory_defaults_to_root_at_secrets(tmp_path: Path):
    """Empty means unset (``:-``): the plan names /root/at-secrets."""
    result, calls, _, _ = _run(
        tmp_path, args=("--dry-run",), extra_env={"AT_SECRETS_DIRECTORY": ""}
    )
    assert result.returncode == 0, result.stderr
    assert "env file (AT_RUNTIME_ENV_FILE): /root/at-secrets/.env.api" in result.stdout
    assert (
        "env file (AT_SECRETS_ENV_FILE): /root/at-secrets/.env.scheduler"
        in result.stdout
    )
    assert [c for c in calls if c[0] in MUTATING] == []


def test_env_files_in_the_run_directory_alone_are_refused(tmp_path: Path):
    """The #2219 default (${RUN_DIRECTORY}/.env.*) is no longer consulted."""
    empty = tmp_path / "empty-secrets"
    empty.mkdir()
    result, calls, state, run_dir = _run(
        tmp_path, extra_env={"AT_SECRETS_DIRECTORY": str(empty)}
    )
    # The harness still writes both files into the run directory.
    assert (run_dir / ".env.api").is_file() and (run_dir / ".env.scheduler").is_file()
    assert result.returncode == 78
    assert (
        f"required env file is unavailable: {empty / '.env.api'} "
        "(set AT_RUNTIME_ENV_FILE to the host file)"
    ) in result.stderr
    assert [c for c in calls if c[0] in MUTATING] == []
    for unit in INITIAL:
        assert state[unit] == _expected(unit), unit


# --- dry-run shows the inputs a real run would refuse ------------------------


def test_dry_run_reports_inputs_and_flags_missing_ones_without_mutation(
    tmp_path: Path,
):
    script = _install(tmp_path, with_template=False)
    empty = tmp_path / "empty-secrets"
    empty.mkdir()
    result, calls, _, _ = _run(
        tmp_path,
        args=("--dry-run",),
        deploy=script,
        extra_env={**NO_ENV_TEMPLATE, "AT_SECRETS_DIRECTORY": str(empty)},
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"env file (AT_RUNTIME_ENV_FILE): {empty / '.env.api'} MISSING" in out
    assert f"env file (AT_SECRETS_ENV_FILE): {empty / '.env.scheduler'} MISSING" in out
    assert "haproxy template: MISSING or ambiguous; a real run exits 78" in out
    assert str(script.parent / "haproxy.cfg.tmpl") in out
    assert [c for c in calls if c[0] in MUTATING] == []


def test_dry_run_reports_resolved_inputs(tmp_path: Path):
    script = _install(tmp_path, with_template=True)
    result, _, _, run_dir = _run(
        tmp_path, args=("--dry-run",), deploy=script, extra_env=NO_ENV_TEMPLATE
    )
    assert result.returncode == 0, result.stderr
    assert f"env file (AT_RUNTIME_ENV_FILE): {run_dir / '.env.api'}\n" in result.stdout
    assert (
        f"env file (AT_SECRETS_ENV_FILE): {run_dir / '.env.scheduler'}\n"
        in result.stdout
    )
    assert (
        f"haproxy template (installed): {script.parent / 'haproxy.cfg.tmpl'}\n"
        in result.stdout
    )


# --- (c) the runbook's canonical invocation matches the script ---------------


def _canonical_block() -> str:
    text = RUNBOOK.read_text()
    match = re.search(r"## Canonical NCP invocation.*?```bash\n(.*?)```", text, re.S)
    assert match, "runbook has no canonical invocation block"
    return match.group(1)


def test_runbook_canonical_invocation_uses_only_real_inputs() -> None:
    block = _canonical_block()
    body = DEPLOY.read_text()
    assert "/root/at-run/deploy-ncp-pull.sh" in block
    for var in re.findall(r"\b([A-Z][A-Z0-9_]+)=", block):
        assert f"${{{var}" in body, f"runbook sets {var}, the script never reads it"
    for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", block):
        assert re.search(rf"^\s+{re.escape(flag)}[)=]", body, re.M), flag
    # Deploy #4 (2026-10-17) measured paths; nothing under /root/at-run/.env*.
    assert "/root/at-run/.env" not in block
    assert "/root/at-run/haproxy.cfg.tmpl" not in block
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    # The default form: no env override and no template flag at all.
    assert "/root/at-run/deploy-ncp-pull.sh sha-abcdef0 --dry-run" in lines
    assert "/root/at-run/deploy-ncp-pull.sh sha-abcdef0" in lines
    # One fully explicit form spelling out every measured path.
    assert "AT_RUNTIME_ENV_FILE=/root/at-secrets/.env.api \\" in lines
    assert "AT_SECRETS_ENV_FILE=/root/at-secrets/.env.scheduler \\" in lines
    assert (
        "--haproxy-template /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl --dry-run"
        in lines
    )


# --- (d) the measured NCP layout under a fake root (deploy #4, 2026-10-17) ----


def _ncp_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Rebuild the measured host tree: <root>/at-run is the run directory.

    /root/at-run/deploy-ncp-pull.sh            installed script
    /root/at-run/ops/ncp/haproxy/haproxy.cfg.tmpl  template (no sibling copy)
    /root/at-secrets/.env.api, .env.scheduler   env files
    The harness's run directory is ``tmp_path/run``, so it stands in for
    /root/at-run and ``tmp_path`` for /root.
    """
    run_dir = tmp_path / "run"
    template = run_dir / "ops/ncp/haproxy/haproxy.cfg.tmpl"
    template.parent.mkdir(parents=True)
    shutil.copy2(TEMPLATE, template)
    script = run_dir / "deploy-ncp-pull.sh"
    shutil.copy2(DEPLOY, script)
    script.chmod(0o750)
    secrets = tmp_path / "at-secrets"
    secrets.mkdir()
    (secrets / ".env.api").write_text("x=y\n")
    (secrets / ".env.scheduler").write_text(_token_lines())
    return script, template, secrets


def test_measured_ncp_layout_deploys_with_defaults_only(tmp_path: Path):
    script, template, secrets = _ncp_root(tmp_path)
    result, calls, state, run_dir = _run(
        tmp_path,
        deploy=script,
        extra_env={**NO_ENV_TEMPLATE, "AT_SECRETS_DIRECTORY": str(secrets)},
    )
    assert result.returncode == 0, result.stderr
    assert not (run_dir / "haproxy.cfg.tmpl").exists()
    assert f"haproxy template (run-ops): {template}\n" in result.stdout
    want = [str(secrets / ".env.api"), str(secrets / ".env.scheduler")]
    assert f"env file 1 (AT_RUNTIME_ENV_FILE): {want[0]}\n" in result.stdout
    assert f"env file 2 (AT_SECRETS_ENV_FILE): {want[1]}\n" in result.stdout
    app_runs = [c for c in calls if c[0] == "run" and any(a.startswith(NEW) for a in c)]
    assert len(app_runs) >= len(INITIAL)
    for call in app_runs:
        assert _env_files(call) == want, call
    _rendered_matches_template(run_dir, template)
    assert state["at-api-green"] == NEW
    assert (run_dir / "deployed-digest").read_text() == NEW + "\n"


def test_measured_ncp_layout_dry_run_resolves_every_input(tmp_path: Path):
    script, template, secrets = _ncp_root(tmp_path)
    result, calls, _, _ = _run(
        tmp_path,
        args=("--dry-run",),
        deploy=script,
        extra_env={**NO_ENV_TEMPLATE, "AT_SECRETS_DIRECTORY": str(secrets)},
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"env file (AT_RUNTIME_ENV_FILE): {secrets / '.env.api'}\n" in out
    assert f"env file (AT_SECRETS_ENV_FILE): {secrets / '.env.scheduler'}\n" in out
    assert f"haproxy template (run-ops): {template}\n" in out
    assert "MISSING" not in out
    assert [c for c in calls if c[0] in MUTATING] == []


def test_measured_ncp_layout_explicit_overrides_still_win(tmp_path: Path):
    """Explicit env files and template beat the measured defaults."""
    script, _, _ = _ncp_root(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    api, scheduler = other / "a.env", other / "b.env"
    api.write_text("x=y\n")
    scheduler.write_text(_token_lines())
    chosen = other / "chosen.tmpl"
    shutil.copy2(TEMPLATE, chosen)
    result, calls, _, _ = _run(
        tmp_path,
        args=("--haproxy-template", str(chosen)),
        deploy=script,
        extra_env={
            "MCP_HAPROXY_TEMPLATE": str(tmp_path / "absent.tmpl"),
            "AT_RUNTIME_ENV_FILE": str(api),
            "AT_SECRETS_ENV_FILE": str(scheduler),
        },
    )
    assert result.returncode == 0, result.stderr
    assert f"haproxy template (--haproxy-template): {chosen}\n" in result.stdout
    app_runs = [c for c in calls if c[0] == "run" and any(a.startswith(NEW) for a in c)]
    assert app_runs
    for call in app_runs:
        assert _env_files(call) == [str(api), str(scheduler)], call


def test_run_ops_and_installed_templates_together_are_refused(tmp_path: Path):
    script, template, secrets = _ncp_root(tmp_path)
    sibling = script.parent / "haproxy.cfg.tmpl"
    shutil.copy2(TEMPLATE, sibling)
    result, calls, state, run_dir = _run(
        tmp_path,
        deploy=script,
        extra_env={**NO_ENV_TEMPLATE, "AT_SECRETS_DIRECTORY": str(secrets)},
    )
    assert result.returncode == 78
    assert "HAProxy template is ambiguous" in result.stderr
    assert str(sibling) in result.stderr and str(template) in result.stderr
    _assert_untouched(calls, state, run_dir)


def test_checkout_reaching_the_run_ops_template_counts_once(tmp_path: Path):
    """Script at <run>/scripts/: the checkout and run-ops paths are one file."""
    _, template, secrets = _ncp_root(tmp_path)
    script = tmp_path / "run" / "scripts" / "deploy-ncp-pull.sh"
    script.parent.mkdir()
    shutil.copy2(DEPLOY, script)
    result, _, _, run_dir = _run(
        tmp_path,
        deploy=script,
        extra_env={**NO_ENV_TEMPLATE, "AT_SECRETS_DIRECTORY": str(secrets)},
    )
    assert result.returncode == 0, result.stderr
    assert f"haproxy template (run-ops): {template}\n" in result.stdout
    _rendered_matches_template(run_dir, template)


def test_measured_layout_without_template_names_every_candidate(tmp_path: Path):
    script, template, secrets = _ncp_root(tmp_path)
    template.unlink()
    result, calls, state, run_dir = _run(
        tmp_path,
        deploy=script,
        extra_env={**NO_ENV_TEMPLATE, "AT_SECRETS_DIRECTORY": str(secrets)},
    )
    assert result.returncode == 78
    assert "HAProxy template is unavailable" in result.stderr
    for path in (
        run_dir / "haproxy.cfg.tmpl",
        run_dir / "ops/ncp/haproxy/haproxy.cfg.tmpl",
        tmp_path / "ops/ncp/haproxy/haproxy.cfg.tmpl",
    ):
        assert str(path) in result.stderr, path
    _assert_untouched(calls, state, run_dir)
