from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "app" / "templates"


def test_design_system_artifacts_are_imported() -> None:
    assert (ROOT / "docs/design-system/README.md").is_file()
    assert (ROOT / "docs/design-system/SKILL.md").is_file()
    assert (ROOT / "docs/design-system/preview/01-surfaces.html").is_file()
    assert (ROOT / "app/static/css/colors_and_type.css").is_file()
    assert (ROOT / "app/static/css/fonts/PretendardVariable.ttf").is_file()
    assert (ROOT / "app/static/css/auth.css").is_file()


def test_design_system_preview_pages_have_document_metadata() -> None:
    for preview in (ROOT / "docs/design-system/preview").glob("*.html"):
        html = preview.read_text(encoding="utf-8")
        assert '<html lang="ko">' in html
        assert "<title>" in html


def test_type_previews_use_runtime_font_asset() -> None:
    expected = "../../app/static/css/fonts/PretendardVariable.ttf"
    for preview in [
        "docs/design-system/preview/03-type.html",
        "docs/design-system/preview/03b-type-weights.html",
    ]:
        html = (ROOT / preview).read_text(encoding="utf-8")
        assert expected in html
        assert "../fonts/PretendardVariable.ttf" not in html


def test_icon_preview_is_self_contained() -> None:
    html = (ROOT / "docs/design-system/preview/12-iconography.html").read_text(
        encoding="utf-8"
    )
    assert "https://cdn.jsdelivr.net" not in html
    assert "<link rel=" not in html


def test_auth_templates_share_static_styles() -> None:
    for template in ["login.html", "register.html"]:
        html = (TEMPLATE_DIR / template).read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/static/css/auth.css">' in html
        assert ".auth-page {" not in html


def test_static_route_is_mounted_for_runtime_css() -> None:
    from app.main import api

    assert any(getattr(route, "name", None) == "static" for route in api.routes)


def test_runtime_css_assets_are_publicly_served() -> None:
    from fastapi.testclient import TestClient

    from app.main import api

    with TestClient(api) as client:
        token_response = client.get(
            "/static/css/colors_and_type.css", follow_redirects=False
        )
        auth_response = client.get("/static/css/auth.css", follow_redirects=False)

    assert token_response.status_code == 200
    assert "text/css" in token_response.headers["content-type"]
    assert "--accent" in token_response.text
    assert auth_response.status_code == 200
    assert "text/css" in auth_response.headers["content-type"]
    assert ".auth-page" in auth_response.text


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
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(("html", "xml")),
    )
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
