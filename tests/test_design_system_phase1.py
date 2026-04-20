from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "app" / "templates"


def test_design_system_artifacts_are_imported() -> None:
    assert (ROOT / "docs/design-system/README.md").is_file()
    assert (ROOT / "docs/design-system/SKILL.md").is_file()
    assert (ROOT / "docs/design-system/preview/01-surfaces.html").is_file()
    assert (ROOT / "app/static/css/colors_and_type.css").is_file()
    assert (ROOT / "app/static/css/fonts/PretendardVariable.ttf").is_file()


def test_static_route_is_mounted_for_runtime_css() -> None:
    from app.main import api

    assert any(getattr(route, "name", None) == "static" for route in api.routes)


def test_runtime_css_is_publicly_served() -> None:
    from fastapi.testclient import TestClient

    from app.main import api

    with TestClient(api) as client:
        response = client.get("/static/css/colors_and_type.css", follow_redirects=False)

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert "--accent" in response.text


def test_common_entrypoints_load_shared_design_tokens() -> None:
    expected = "/static/css/colors_and_type.css"
    for template in [
        "base.html",
        "screener/_layout.html",
        "portfolio_dashboard.html",
    ]:
        html = (TEMPLATE_DIR / template).read_text(encoding="utf-8")
        assert expected in html


def test_legacy_templates_parse_after_migration() -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    for template in [
        "base.html",
        "nav.html",
        "login.html",
        "register.html",
        "admin_users.html",
        "error.html",
    ]:
        env.parse((TEMPLATE_DIR / template).read_text(encoding="utf-8"))


def test_legacy_templates_do_not_use_old_purple_gradient() -> None:
    forbidden = ("#667eea", "#764ba2")
    for template in [
        "base.html",
        "login.html",
        "register.html",
        "admin_users.html",
        "error.html",
        "nav.html",
    ]:
        html = (TEMPLATE_DIR / template).read_text(encoding="utf-8")
        for value in forbidden:
            assert value not in html
