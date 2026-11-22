"""hk 6037: the gate's anonymous evidence mode, --main-ancestor, --expect-sha, --resolve-only.

The pi has no gh auth and re-verifies the deploy tag itself. Everything here is offline:
api.github.com is a fake opener, the registry is a fake `_inspect`.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from scripts import deploy_ci_gate as gate

SHA = "a" * 40
OTHER = "b" * 40
DIGEST = "sha256:" + "1" * 64
PINNED = f"{gate.IMAGE_REPOSITORY}@{DIGEST}"
CHECKS = ("lint", "taskiq-smoke", *(f"test (3.13, {n})" for n in range(1, 5)))


class FakeResponse:
    def __init__(self, body: object, link: str = "") -> None:
        self._body = json.dumps(body).encode()
        self.headers = {"Link": link}

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeGitHub:
    """Serves canned api.github.com answers by URL substring; records every request."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    def build_opener(self, *_handlers: object) -> FakeGitHub:
        return self

    def open(self, request, timeout: float = 0):  # noqa: ANN001 - urllib signature
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        for needle, answer in self.routes.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected GitHub request: {url}")


def green_routes(compare_status: str = "ahead") -> dict[str, object]:
    return {
        "/actions/workflows/test.yml/runs?": FakeResponse(
            {
                "workflow_runs": [
                    {"id": 100, "run_attempt": 1, "head_sha": SHA, "event": "push"}
                ]
            }
        ),
        "/actions/runs/100/attempts/1/jobs?": FakeResponse(
            {
                "jobs": [
                    {"id": n, "name": name, "status": "completed", "conclusion": "success"}
                    for n, name in enumerate(CHECKS, 1)
                ]
            }
        ),
        f"/compare/{SHA}...main": FakeResponse({"status": compare_status}),
    }


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AT_DEPLOY_GATE_GITHUB", "gh")  # restored after the test
    monkeypatch.setattr(
        gate, "_json_command", lambda argv: pytest.fail(f"gh must not run: {argv}")
    )

    def install(routes: dict[str, object]) -> FakeGitHub:
        fake = FakeGitHub(routes)
        monkeypatch.setattr(gate.urllib.request, "build_opener", fake.build_opener)
        return fake

    return install


def image(monkeypatch: pytest.MonkeyPatch, sha: str = SHA) -> None:
    monkeypatch.setattr(gate, "resolve_image", lambda _image: (PINNED, [PINNED]))
    monkeypatch.setattr(gate, "image_sha", lambda _refs: sha)


def test_anonymous_mode_reads_the_public_api_without_credentials(
    github, monkeypatch, capsys
):
    fake = github(green_routes())
    assert gate.main(["--github", "anonymous", "--sha", SHA]) == 0
    assert f"PASS SHA {SHA}" in capsys.readouterr().err
    assert fake.requests, "nothing was read"
    for url, headers in fake.requests:
        assert url.startswith("https://api.github.com/repos/mgh3326/auto_trader/")
        assert not {k.lower() for k in headers} & {"authorization", "cookie"}


def test_anonymous_mode_follows_link_pages_and_refuses_foreign_next_links(
    github, monkeypatch
):
    routes = green_routes()
    first = FakeResponse(
        {
            "workflow_runs": [
                {"id": 100, "run_attempt": 1, "head_sha": SHA, "event": "push"}
            ]
        },
        link='<https://api.github.com/repos/x/y/runs?page=2>; rel="next"',
    )
    routes["/actions/workflows/test.yml/runs?"] = first
    routes["runs?page=2"] = FakeResponse({"workflow_runs": []})
    fake = github(routes)
    assert gate.main(["--github", "anonymous", "--sha", SHA]) == 0
    assert any("page=2" in url for url, _ in fake.requests)

    routes["/actions/workflows/test.yml/runs?"] = FakeResponse(
        {"workflow_runs": []}, link='<https://evil.example/x>; rel="next"'
    )
    github(routes)
    assert gate.main(["--github", "anonymous", "--sha", SHA]) == 78


@pytest.mark.parametrize("code", [403, 404, 500])
def test_anonymous_http_errors_are_unverified_not_green(github, capsys, code):
    routes = green_routes()
    routes["/actions/workflows/test.yml/runs?"] = urllib.error.HTTPError(
        "https://api.github.com/x", code, "boom", {}, io.BytesIO(b"")
    )
    github(routes)
    assert gate.main(["--github", "anonymous", "--sha", SHA]) == 78
    err = capsys.readouterr().err
    assert f"unverified (GitHub evidence unavailable (HTTP {code}))" in err


def test_env_selects_the_mode_and_a_bad_value_refuses(github, monkeypatch, capsys):
    github(green_routes())
    monkeypatch.setenv("AT_DEPLOY_GATE_GITHUB", "anonymous")
    assert gate.main(["--sha", SHA]) == 0  # env alone, no flag
    monkeypatch.setenv("AT_DEPLOY_GATE_GITHUB", "token")
    assert gate.main(["--sha", SHA]) == 78


def test_main_ancestor_accepts_ahead_and_identical(github, monkeypatch):
    for status in ("ahead", "identical"):
        github(green_routes(status))
        image(monkeypatch)
        assert (
            gate.main(
                ["--github", "anonymous", "--image", f"{gate.IMAGE_REPOSITORY}:deploy", "--main-ancestor"]
            )
            == 0
        )


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_a_commit_off_main_is_refused_even_with_a_green_suite(
    github, monkeypatch, capsys, status
):
    github(green_routes(status))
    image(monkeypatch)
    rc = gate.main(
        ["--github", "anonymous", "--image", f"{gate.IMAGE_REPOSITORY}:deploy", "--main-ancestor"]
    )
    assert rc == 78
    err = capsys.readouterr().err
    assert "main-ancestor" in err and status in err and "PASS" not in err


def test_allow_unverified_never_waives_main_ancestry(github, monkeypatch, capsys):
    routes = green_routes("diverged")
    routes["/actions/workflows/test.yml/runs?"] = FakeResponse({"workflow_runs": []})
    github(routes)
    image(monkeypatch)
    rc = gate.main(
        [
            "--github",
            "anonymous",
            "--image",
            f"{gate.IMAGE_REPOSITORY}:deploy",
            "--main-ancestor",
            "--allow-unverified",
            "emergency",
        ]
    )
    assert rc == 78  # CI evidence may be waived; which commit this is may not


def test_unreadable_ancestry_evidence_refuses(github, monkeypatch):
    routes = green_routes()
    routes[f"/compare/{SHA}...main"] = FakeResponse({"no": "status"})
    github(routes)
    image(monkeypatch)
    assert (
        gate.main(
            ["--github", "anonymous", "--image", f"{gate.IMAGE_REPOSITORY}:deploy", "--main-ancestor"]
        )
        == 78
    )


def test_expect_sha_must_match_the_images_revision(github, monkeypatch, capsys):
    github(green_routes())
    image(monkeypatch, sha=OTHER)
    rc = gate.main(
        ["--github", "anonymous", "--image", f"{gate.IMAGE_REPOSITORY}:deploy", "--expect-sha", SHA]
    )
    assert rc == 78
    assert "is not the expected" in capsys.readouterr().err


def test_resolve_only_prints_digest_and_sha_and_touches_no_github(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        gate, "_api_pages", lambda _e: pytest.fail("GitHub must not be read")
    )
    image(monkeypatch)
    assert (
        gate.main(["--image", f"{gate.IMAGE_REPOSITORY}:deploy", "--resolve-only"]) == 0
    )
    assert capsys.readouterr().out == f"{PINNED}\t{SHA}\n"


def test_default_mode_still_uses_gh(monkeypatch, capsys):
    monkeypatch.delenv("AT_DEPLOY_GATE_GITHUB", raising=False)
    seen: list[list[str]] = []

    def command(argv: list[str]):
        seen.append(argv)
        if "/workflows/test.yml/runs?" in argv[-1]:
            return [{"workflow_runs": [{"id": 100, "run_attempt": 1, "head_sha": SHA, "event": "push"}]}]
        return [
            {
                "jobs": [
                    {"id": n, "name": name, "status": "completed", "conclusion": "success"}
                    for n, name in enumerate(CHECKS, 1)
                ]
            }
        ]

    monkeypatch.setattr(gate, "_json_command", command)
    assert gate.main(["--sha", SHA]) == 0
    assert all(argv[0] == "gh" for argv in seen)
