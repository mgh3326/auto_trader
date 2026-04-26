import pytest

from app.services.order_intent_discord_brief import build_decision_desk_url


@pytest.mark.unit
def test_build_decision_desk_url_strips_trailing_slash() -> None:
    url = build_decision_desk_url("https://trader.robinco.dev/", "decision-r1")
    assert url == "https://trader.robinco.dev/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_build_decision_desk_url_local_origin() -> None:
    url = build_decision_desk_url("http://localhost:8000", "decision-r1")
    assert url == "http://localhost:8000/portfolio/decision?run_id=decision-r1"


@pytest.mark.unit
def test_build_decision_desk_url_percent_encodes_run_id() -> None:
    url = build_decision_desk_url(
        "https://trader.robinco.dev/", "decision-abc/with slash"
    )
    assert url == (
        "https://trader.robinco.dev/portfolio/decision"
        "?run_id=decision-abc%2Fwith%20slash"
    )
