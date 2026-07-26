from __future__ import annotations

import wcmb_ranking as wr


def test_rank_symbols_sorts_score_descending():
    scored = {"AAA/USD": 0.01, "BBB/USD": 0.05, "CCC/USD": 0.03}
    ranks = wr.rank_symbols(scored)
    assert ranks == {"BBB/USD": 1, "CCC/USD": 2, "AAA/USD": 3}


def test_rank_symbols_tie_break_is_symbol_ascending_not_insertion_order():
    scored = {"ZZZ/USD": 0.02, "AAA/USD": 0.02, "MMM/USD": 0.02}
    ranks = wr.rank_symbols(scored)
    assert ranks == {"AAA/USD": 1, "MMM/USD": 2, "ZZZ/USD": 3}


def test_rank_symbols_mixed_ties_and_distinct_scores():
    scored = {"A": 0.10, "B": 0.05, "C": 0.05, "D": 0.20}
    ranks = wr.rank_symbols(scored)
    assert ranks == {"D": 1, "A": 2, "B": 3, "C": 4}


def test_classify_held_symbol_score_zero_exits_not_positive_filter_boundary():
    # AC14: positive filter Score>0 fixed -- Score==0 is NOT "> 0".
    assert wr.classify_held_symbol(score=0.0, rank=1, k=5, b=1) == "EXIT"


def test_classify_held_symbol_negative_score_exits_regardless_of_rank():
    assert wr.classify_held_symbol(score=-0.01, rank=1, k=5, b=1) == "EXIT"


def test_classify_held_symbol_rank_boundary_k_plus_b_holds():
    # AC19: rank == k+b holds.
    assert wr.classify_held_symbol(score=0.01, rank=6, k=5, b=1) == "HOLD"


def test_classify_held_symbol_rank_boundary_k_plus_b_plus_1_exits():
    # AC19: rank == k+b+1 exits.
    assert wr.classify_held_symbol(score=0.01, rank=7, k=5, b=1) == "EXIT"


def test_classify_held_symbol_well_within_k_holds():
    assert wr.classify_held_symbol(score=0.01, rank=1, k=5, b=1) == "HOLD"
