"""Acceptance 2: the feature join refuses available_at after the decision time."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from research.kr_boosting.config import TEST_START
from research.kr_boosting.eligibility import d3_signals, wilder_rsi
from research.kr_boosting.exposure_audit import provably_untouched, run_audit
from research.kr_boosting.features import (
    FEATURE_NAMES,
    compute_features,
    decision_times,
    feature_join,
)
from research.kr_boosting.panel import (
    NAT,
    ExportIdentityError,
    SealedPeriodRead,
    build_panel,
    read_rows,
    verify_export,
)
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.indicators import rsi_wilder
from research.kr_corpus.d3_engine.models import Bar
from research.kr_delist_repair.model import AvailabilityViolation
from tests.research.eval_contract.helpers import ticks
from tests.research.kr_boosting.helpers import (
    flat_rows,
    frame,
    random_walk_rows,
    row,
    weekdays,
)

SESSIONS = weekdays(date(2019, 1, 1), 90)


def _panel():
    return build_panel(frame(random_walk_rows("000001", SESSIONS, 7)))


def test_join_refuses_a_row_available_after_the_decision_time() -> None:
    panel = _panel()
    store = compute_features(panel)
    t = 70
    # The bar of session t is published only after the next session's pre-open.
    panel.available_at[0, t] = decision_times(panel)[t] + 1
    with pytest.raises(AvailabilityViolation, match="after decision time"):
        feature_join(panel, store, np.array([0]), np.array([t]))


def test_join_refuses_one_bad_row_in_a_batch_rather_than_dropping_it() -> None:
    panel = _panel()
    store = compute_features(panel)
    panel.available_at[0, 75] = decision_times(panel)[75] + 1
    with pytest.raises(AvailabilityViolation):
        feature_join(panel, store, np.array([0, 0, 0]), np.array([70, 75, 80]))


def test_join_refuses_an_explicit_earlier_decision_time() -> None:
    panel = _panel()
    store = compute_features(panel)
    t = 70
    earlier = np.array([panel.available_at[0, t] - 1])
    with pytest.raises(AvailabilityViolation):
        feature_join(panel, store, np.array([0]), np.array([t]), decision_time=earlier)


def test_join_refuses_a_late_row_inside_the_lookback_window() -> None:
    panel = _panel()
    store = compute_features(panel)
    t = 70
    panel.available_at[0, t - 5] = decision_times(panel)[t] + 1
    with pytest.raises(AvailabilityViolation, match="features read"):
        feature_join(panel, store, np.array([0]), np.array([t]))


def test_join_refuses_a_late_peer_row_behind_a_market_feature() -> None:
    rows = random_walk_rows("000001", SESSIONS, 7) + random_walk_rows(
        "000002", SESSIONS, 8
    )
    panel = build_panel(frame(rows))
    store = compute_features(panel)
    t = 70
    panel.available_at[1, t] = decision_times(panel)[t] + 1
    with pytest.raises(AvailabilityViolation, match="features read"):
        feature_join(panel, store, np.array([0]), np.array([t]))


def test_join_accepts_available_exactly_at_the_decision_time() -> None:
    panel = _panel()
    store = compute_features(panel)
    t = 70
    exact = np.array([panel.available_at[0, t]])
    out = feature_join(panel, store, np.array([0]), np.array([t]), decision_time=exact)
    assert len(out.frame) == 1


def test_row_without_available_at_is_excluded_and_counted() -> None:
    panel = _panel()
    store = compute_features(panel)
    panel.available_at[0, 70] = NAT
    out = feature_join(panel, store, np.array([0, 0]), np.array([70, 71]))
    assert out.excluded_no_available_at == 1
    assert out.frame["t"].tolist() == [71]


def test_join_refuses_a_row_on_the_last_session() -> None:
    panel = _panel()
    store = compute_features(panel)
    with pytest.raises(AvailabilityViolation, match="no decision time"):
        feature_join(panel, store, np.array([0]), np.array([len(SESSIONS) - 1]))


def test_a_real_session_decides_after_its_bar_is_published() -> None:
    panel = _panel()
    decide = decision_times(panel)
    # 16:00 KST of t is before 08:30 KST of t+1 for every session with a decision.
    assert (panel.available_at[0, :-1] <= decide[:-1]).all()
    assert decide[-1] == NAT


def test_features_cover_sixty_completed_sessions() -> None:
    panel = _panel()
    store = compute_features(panel)
    out = feature_join(panel, store, np.array([0]), np.array([80])).frame
    assert out["ret_lag_1"].iloc[0] == pytest.approx(store.ret[0, 80])
    assert out["ret_lag_60"].iloc[0] == pytest.approx(store.ret[0, 21])
    assert out["logtv_lag_60"].iloc[0] == pytest.approx(store.logtv[0, 21])
    assert sum(n.startswith("ret_lag_") for n in FEATURE_NAMES) == 60
    assert sum(n.startswith("logtv_lag_") for n in FEATURE_NAMES) == 60


def test_future_rows_cannot_change_a_feature() -> None:
    clean = _panel()
    dirty = _panel()
    t = 70
    dirty.close[0, t + 1 :] *= 3.0
    dirty.high[0, t + 1 :] *= 3.0
    dirty.low[0, t + 1 :] *= 3.0
    dirty.volume[0, t + 1 :] *= 9.0
    a = feature_join(clean, compute_features(clean), np.array([0]), np.array([t])).frame
    b = feature_join(dirty, compute_features(dirty), np.array([0]), np.array([t])).frame
    np.testing.assert_array_equal(a[list(FEATURE_NAMES)], b[list(FEATURE_NAMES)])


def test_terminal_columns_cannot_change_a_feature() -> None:
    rows = random_walk_rows("000001", SESSIONS, 7)
    plain = build_panel(frame(rows))
    marked = [dict(r) for r in rows]
    marked[-1] |= {
        "is_terminal_session": True,
        "terminal_outcome": "statutory_cause",
        "payout_semantics": "ambiguous",
        "terminal_available_at": datetime(2019, 1, 2, tzinfo=UTC),
    }
    panel = build_panel(frame(marked))
    t = np.array([70])
    a = feature_join(plain, compute_features(plain), np.array([0]), t).frame
    b = feature_join(panel, compute_features(panel), np.array([0]), t).frame
    np.testing.assert_array_equal(a[list(FEATURE_NAMES)], b[list(FEATURE_NAMES)])


def test_missing_bars_stay_unknown_never_zero() -> None:
    rows = random_walk_rows("000001", SESSIONS, 7)
    rows[60] = row("000001", SESSIONS[60], 0, 0, 0, 0, state="invalid_ohlc")
    rows[61] = row("000001", SESSIONS[61], 0, 0, 0, 10_000, state="no_trade")
    panel = build_panel(frame(rows))
    store = compute_features(panel)
    assert np.isnan(store.ret[0, 60])
    assert np.isnan(store.ret[0, 61])
    assert np.isnan(store.ret[0, 62])  # its previous close is not a traded close


def test_sealed_period_is_refused_before_any_file_read(tmp_path: Path) -> None:
    with pytest.raises(SealedPeriodRead):
        read_rows(tmp_path / "does-not-exist.parquet", last_session=TEST_START)


def test_hash_mismatch_is_refused(tmp_path: Path) -> None:
    (tmp_path / "kr_daily_causal.v1.parquet").write_bytes(b"not the export")
    (tmp_path / "manifest.json").write_text(
        '{"files": {"kr_daily_causal": {"sha256": "' + "0" * 64 + '", "rows": 1}}}'
    )
    with pytest.raises(ExportIdentityError):
        verify_export(tmp_path)


# ---------------------------------------------------------- D3 reproduction


def _bars(rows: list[dict[str, object]]) -> list[Bar]:
    return [
        Bar(
            r["session"],
            r["symbol"],
            Decimal(int(r["open"])),
            Decimal(int(r["high"])),
            Decimal(int(r["low"])),
            Decimal(int(r["close"])),
        )
        for r in rows
        if r["session_state"] == "valid"
    ]


def test_float_rsi_matches_the_d3_recursion() -> None:
    closes = [float(r["close"]) for r in random_walk_rows("000001", SESSIONS, 3)]
    ours = wilder_rsi(np.array(closes))
    theirs = rsi_wilder([Decimal(int(c)) for c in closes])
    for mine, exact in zip(ours, theirs, strict=True):
        if exact is None:
            assert np.isnan(mine)
        else:
            assert mine == pytest.approx(float(exact), abs=1e-9)


def test_signals_equal_the_d3_engine_on_fixture_bars() -> None:
    sessions = weekdays(date(2017, 1, 2), 420)
    rows: list[dict[str, object]] = []
    for i, symbol in enumerate(("000001", "000002", "000003", "000004")):
        rows += random_walk_rows(symbol, sessions, 11 + i, drift=0.0)
    # A break in one listing restarts its contiguous history, as in D3.
    rows = [
        row("000002", r["session"], 0, 0, 0, 0, state="invalid_ohlc")
        if r["symbol"] == "000002" and r["session"] == sessions[300]
        else r
        for r in rows
    ]
    panel = build_panel(frame(rows))
    ours = {(panel.listing_ids[s.li], s.t): s for s in d3_signals(panel)}

    engine = PortfolioEngine(ticks())
    theirs: dict[tuple[str, int], tuple[Decimal, Decimal]] = {}
    for symbol in ("000001", "000002", "000003", "000004"):
        listing = f"{symbol}#0"
        li = panel.listing_ids.index(listing)
        by_session = {
            b.session: b for b in _bars([r for r in rows if r["symbol"] == symbol])
        }
        start = 0
        for t in range(len(sessions) - 1):
            if sessions[t] not in by_session:
                start = t + 1
                continue
            s_index = t + 1
            history = [
                by_session[d] for d in sessions[start : s_index + 1] if d in by_session
            ]
            if sessions[s_index] not in by_session:
                continue
            signal = engine._signal_for_session(history, len(history) - 1)
            if signal is not None:
                theirs[(listing, t)] = (signal[0], signal[1])
        assert li >= 0

    # D3 also requires a bar on S; ours cannot know that before S trades.
    ours_with_next = {
        key: value
        for key, value in ours.items()
        if panel.state[panel.listing_ids.index(key[0]), key[1] + 1] == 1
    }
    assert len(theirs) >= 5, "fixture must produce real signals"
    assert set(ours_with_next) == set(theirs)
    for key, (rsi, l2) in theirs.items():
        assert ours_with_next[key].rsi == pytest.approx(float(rsi), abs=1e-4)
        assert ours_with_next[key].l2_price == l2


def test_flat_prices_give_no_signal() -> None:
    panel = build_panel(frame(flat_rows("000001", weekdays(date(2017, 1, 2), 200))))
    assert d3_signals(panel) == []


# ------------------------------------------------------------ package guards

PACKAGE = Path("research/kr_boosting")
FORBIDDEN_ROOTS = {"app", "sqlalchemy", "asyncpg", "taskiq", "httpx", "requests"}


def _imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.col_offset) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.col_offset))
    return found


def test_package_imports_no_runtime_broker_or_db_surface() -> None:
    for path in PACKAGE.glob("*.py"):
        for name, _ in _imports(path):
            assert name.split(".")[0] not in FORBIDDEN_ROOTS, f"{path}: {name}"


def test_only_train_imports_lightgbm_and_only_lazily() -> None:
    for path in PACKAGE.glob("*.py"):
        for name, col in _imports(path):
            if name.split(".")[0] in {"lightgbm", "xgboost", "sklearn"}:
                assert path.name == "train.py", path
                assert col > 0, "the ML import must stay inside a function"


def test_ml_dependencies_are_not_in_the_project() -> None:
    text = Path("pyproject.toml").read_text().lower()
    for name in ("lightgbm", "xgboost", "scikit-learn"):
        assert name not in text


def test_exposure_audit_finds_the_repository_evidence() -> None:
    hits = run_audit(Path("."))
    assert not provably_untouched(hits)
    assert {h.path for h in hits} >= {
        "research/kr_corpus/config.py",
        "research/underwater_spike_trim_study/README.md",
    }


def test_exposure_audit_on_an_empty_tree_finds_nothing(tmp_path: Path) -> None:
    assert run_audit(tmp_path) == ()
