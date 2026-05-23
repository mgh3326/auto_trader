import pytest

from app.services.screener_evidence import scoring


@pytest.mark.parametrize(
    ("change_rate", "expected"),
    [(10.0, 10.0), (0.0, 5.0), (-10.0, 0.0), (4.2, 7.1), (None, 0.0)],
)
def test_momentum_score(change_rate, expected):
    assert scoring.momentum_score(change_rate) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("rsi", "expected"),
    [(30.0, 9.0), (50.0, 5.0), (70.0, 1.0), (10.0, 10.0), (None, 0.0)],
)
def test_oversold_score(rsi, expected):
    assert scoring.oversold_score(rsi) == pytest.approx(expected)


def test_rank_score_descending_positions():
    # 4 items: best gets 10, worst gets 2.5 (10 * (1 - idx/n)).
    assert scoring.rank_score(0, 4) == pytest.approx(10.0)
    assert scoring.rank_score(3, 4) == pytest.approx(2.5)


def test_rank_score_single_item_is_max():
    assert scoring.rank_score(0, 1) == pytest.approx(10.0)


def test_clamp_bounds():
    assert scoring.clamp(12.0) == 10.0
    assert scoring.clamp(-1.0) == 0.0
    assert scoring.clamp(6.3) == 6.3
