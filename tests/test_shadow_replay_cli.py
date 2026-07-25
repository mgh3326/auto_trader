# tests/test_shadow_replay_cli.py
"""Unit tests for the shadow-replay headless `claude -p` driver (ROB-697, M1).

No real `claude -p` subprocess and no DB in these tests: `_one_run` is always
monkeypatched (per resolution #5 — the exact `claude -p --output-format
json` envelope is only verified at operator run-time, see
docs/runbooks/shadow-replay.md).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.shadow_replay.corpus import CorpusItem, CorpusSelection
from app.services.shadow_replay.scoring import extract_decision
from scripts import shadow_replay as sr


@pytest.mark.unit
def test_to_item_shape_maps_top_level_trade_setup_into_evidence_snapshot():
    raw = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "trade_setup": {
            "stop": "125000",
            "target": "135000",
            "headline": {"entry": "129600"},
        },
        "trigger_checklist": ["x"],
    }

    shaped = sr._to_item_shape(raw)

    assert shaped == {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "evidence_snapshot": {
            "trade_setup": {
                "stop": "125000",
                "target": "135000",
                "headline": {"entry": "129600"},
            }
        },
        "trigger_checklist": ["x"],
    }


@pytest.mark.unit
def test_to_item_shape_defaults_missing_fields_to_empty():
    assert sr._to_item_shape({}) == {
        "side": None,
        "max_action": {},
        "evidence_snapshot": {"trade_setup": {}},
        "trigger_checklist": [],
    }


@pytest.mark.unit
def test_to_item_shape_roundtrip_recovers_entry_via_extract_decision():
    """The shape-adapter bug fix: `_PROMPT` instructs the replayed agent to
    emit `trade_setup` at the TOP level, but `extract_decision` reads the
    NESTED `evidence_snapshot.trade_setup`. Calling `extract_decision(raw)`
    directly on a raw claude reply silently drops entry/stop/target because
    `ev.get("trade_setup")` reads an empty `{}`. `_to_item_shape` must fix
    this: after adapting, `entry` (and stop/target) must round-trip.
    """
    raw = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "trade_setup": {
            "stop": "125000",
            "target": "135000",
            "headline": {"entry": "129600"},
        },
        "trigger_checklist": ["earnings_beat"],
    }

    # Prove the bug: calling extract_decision directly on the raw reply
    # (no adapter) drops entry/stop/target — they read off an empty dict.
    unfixed = extract_decision(raw)
    assert unfixed["entry"] is None
    assert unfixed["stop"] is None

    # The fix: adapt first, then score.
    fixed = extract_decision(sr._to_item_shape(raw))
    assert fixed["entry"] == Decimal("129600")
    assert fixed["stop"] == Decimal("125000")
    assert fixed["target"] == Decimal("135000")
    assert fixed["side"] == "buy"
    assert fixed["limit_price"] == Decimal("129600")


@pytest.mark.unit
def test_run_batch_counts_discards_and_summarizes(monkeypatch):
    ref = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "evidence_snapshot": {"trade_setup": {"headline": {"entry": "129600"}}},
        "trigger_checklist": ["x"],
    }
    item = CorpusItem("u1", 1, "i1", "action", "buy_review", extract_decision(ref))
    corpus = CorpusSelection("claude_bundle", [item])

    # The replayed agent emits the TOP-LEVEL `trade_setup` shape (per
    # `_PROMPT`), not the nested `evidence_snapshot.trade_setup` shape used
    # for `reference_decision` above — `run_batch` must route each raw reply
    # through `_to_item_shape` before scoring.
    raw_reply = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "trade_setup": {"headline": {"entry": "129600"}},
        "trigger_checklist": ["x"],
    }
    seq = iter([raw_reply, None, raw_reply])  # one MCP-reset discard in the middle
    monkeypatch.setattr(sr, "_one_run", lambda uuid, model: next(seq))  # noqa: ARG005

    out = sr.run_batch(corpus, k=3, model="claude-opus-4-8", tick=Decimal("100"))

    assert len(out) == 1
    assert out[0]["item_uuid"] == "i1"
    assert out[0]["source"] == "claude_bundle"
    assert out[0]["model"] == "claude-opus-4-8"
    assert out[0]["discarded"] == 1
    assert out[0]["summary"]["k"] == 2  # 3 calls - 1 discard
    assert out[0]["summary"]["fidelity"]["side_rate"] == 1.0
    assert out[0]["summary"]["fidelity"]["same_decision_rate"] == 1.0
    assert out[0]["summary"]["no_action_rate"] == 0.0


@pytest.mark.unit
def test_run_batch_never_calls_real_one_run_without_monkeypatch(monkeypatch):
    """Defense-in-depth: if `_one_run` is monkeypatched to fail loudly on any
    call, `run_batch` over an EMPTY corpus must never invoke it at all."""

    def _boom(uuid, model):  # noqa: ARG001
        raise AssertionError("must not be called for an empty corpus")

    monkeypatch.setattr(sr, "_one_run", _boom)
    corpus = CorpusSelection("claude_bundle", [])

    out = sr.run_batch(corpus, k=3, model="claude-opus-4-8", tick=Decimal("100"))

    assert out == []


@pytest.mark.unit
def test_parser_defaults_are_confirm_false_k5_pinned_model():
    args = sr.build_parser().parse_args([])

    assert args.k == 5
    assert args.confirm is False
    assert args.model == "claude-opus-4-8"
    assert "latest" not in args.model  # must be an exact pinned id


@pytest.mark.unit
def test_parser_accepts_explicit_k():
    args = sr.build_parser().parse_args(["--k", "5"])

    assert args.k == 5
    assert args.model  # model has a pinned default


@pytest.mark.unit
def test_write_report_produces_markdown_table(tmp_path):
    results = [
        {
            "item_uuid": "i1",
            "item_kind": "action",
            "source": "claude_bundle",
            "model": "claude-opus-4-8",
            "discarded": 1,
            "summary": {
                "k": 2,
                "no_action_rate": 0.0,
                "self_same_decision_rate": 1.0,
                "fidelity": {
                    "side_rate": 1.0,
                    "size_band_rate": 1.0,
                    "limit_rate": 1.0,
                    "same_decision_rate": 1.0,
                },
            },
        },
        {
            "item_uuid": "i2",
            "item_kind": "watch",
            "source": "claude_bundle",
            "model": "claude-opus-4-8",
            "discarded": 0,
            "summary": {
                "k": 0,
                "no_action_rate": 0.0,
                "self_same_decision_rate": 0.0,
                "fidelity": None,
            },
        },
    ]
    path = tmp_path / "report.md"

    text = sr.write_report(results, path)

    assert text  # non-empty
    assert path.read_text() == text
    assert "claude_bundle" in text
    assert "claude-opus-4-8" in text
    assert "i1" in text and "i2" in text
    assert "1.000" in text  # side_rate / size_band_rate / etc. for i1
    assert "n/a" in text  # i2 has no reference decision -> fidelity is None


@pytest.mark.unit
def test_write_report_handles_empty_results(tmp_path):
    path = tmp_path / "empty.md"

    text = sr.write_report([], path)

    assert "n/a" in text
    assert "Items:** 0" in text


# --- all_samples_discarded (ROB-697 M1 follow-up: the all-discarded signal) ---
#
# If the `claude --output-format json` envelope assumption is wrong,
# `_one_run` returns `None` for EVERY call and `run_batch` produces a
# normal-looking `discarded == k` row for every item. `all_samples_discarded`
# is the pure predicate `_amain` uses to turn that silent-looking success
# into a loud, non-zero exit (see the `_amain` tests below).


@pytest.mark.unit
def test_all_samples_discarded_true_when_every_attempted_sample_discarded():
    results = [
        {"discarded": 3, "summary": {"k": 0}},
        {"discarded": 2, "summary": {"k": 0}},
    ]

    assert sr.all_samples_discarded(results) is True


@pytest.mark.unit
def test_all_samples_discarded_false_when_some_samples_scored():
    results = [
        {"discarded": 1, "summary": {"k": 2}},  # some scored here
        {"discarded": 3, "summary": {"k": 0}},  # fully discarded here
    ]

    assert sr.all_samples_discarded(results) is False


@pytest.mark.unit
def test_all_samples_discarded_false_when_no_samples_attempted():
    assert sr.all_samples_discarded([]) is False


# --- _amain: --confirm gate + the all-discarded exit-3 signal ---
#
# `_amain` opens a DB session (`AsyncSessionLocal`) and calls
# `select_replay_corpus` unconditionally (even in the dry-plan branch), so
# both must be monkeypatched — no real DB, no real subprocess. `_amain`
# imports both names via a DEFERRED `from ... import ...` specifically so a
# unit test can monkeypatch the source module attribute before calling it
# (see the "Deferred import" comment in `_amain`).


class _FakeAsyncSession:
    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _fake_session_local() -> _FakeAsyncSession:
    return _FakeAsyncSession()


def _fake_corpus() -> CorpusSelection:
    ref = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "evidence_snapshot": {"trade_setup": {"headline": {"entry": "129600"}}},
        "trigger_checklist": ["x"],
    }
    item = CorpusItem("u1", 1, "i1", "action", "buy_review", extract_decision(ref))
    return CorpusSelection("claude_bundle", [item])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_without_confirm_never_calls_run_batch(monkeypatch):
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _fake_session_local)

    async def _fake_select(session, *, min_per_kind):  # noqa: ARG001
        return _fake_corpus()

    monkeypatch.setattr(
        "app.services.shadow_replay.corpus.select_replay_corpus", _fake_select
    )

    def _boom(*args, **kwargs):
        raise AssertionError("run_batch must not be called without --confirm")

    monkeypatch.setattr(sr, "run_batch", _boom)

    args = sr.build_parser().parse_args([])  # confirm defaults to False

    assert await sr._amain(args) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_with_confirm_returns_3_when_all_samples_discarded(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _fake_session_local)

    async def _fake_select(session, *, min_per_kind):  # noqa: ARG001
        return _fake_corpus()

    monkeypatch.setattr(
        "app.services.shadow_replay.corpus.select_replay_corpus", _fake_select
    )

    all_discarded_results = [
        {
            "item_uuid": "i1",
            "item_kind": "action",
            "source": "claude_bundle",
            "model": "claude-opus-4-8",
            "discarded": 3,
            "summary": {
                "k": 0,
                "no_action_rate": 0.0,
                "self_same_decision_rate": 0.0,
                "fidelity": None,
            },
        }
    ]
    monkeypatch.setattr(sr, "run_batch", lambda *a, **kw: all_discarded_results)  # noqa: ARG005

    args = sr.build_parser().parse_args(
        ["--confirm", "--report", str(tmp_path / "report.md")]
    )

    assert await sr._amain(args) == 3


@pytest.mark.unit
def test_extract_decision_json_raw_object():
    got = sr._extract_decision_json(
        '{"side": "buy", "max_action": {"notional": 300000}}'
    )
    assert got == {"side": "buy", "max_action": {"notional": 300000}}


@pytest.mark.unit
def test_extract_decision_json_prose_plus_json_fence_with_nesting():
    # The real operator-run failure mode: opus prepends reasoning and wraps a
    # NESTED object in a ```json fence instead of emitting raw JSON.
    text = (
        "Based on the frozen evidence, correct call is no action.\n\n"
        "```json\n"
        '{"side": null, "max_action": {"notional": null, "limit_price": null}, '
        '"trade_setup": {"headline": {"entry": null}}, "trigger_checklist": ["a", "b"]}\n'
        "```"
    )
    got = sr._extract_decision_json(text)
    assert got is not None
    assert got["side"] is None
    assert got["max_action"] == {"notional": None, "limit_price": None}
    assert got["trade_setup"] == {"headline": {"entry": None}}
    assert got["trigger_checklist"] == ["a", "b"]


@pytest.mark.unit
def test_extract_decision_json_bare_object_in_prose():
    text = 'Reasoning. Decision: {"side": "sell", "trigger_checklist": []} — done.'
    assert sr._extract_decision_json(text) == {"side": "sell", "trigger_checklist": []}


@pytest.mark.unit
def test_extract_decision_json_no_json_returns_none():
    assert sr._extract_decision_json("no json object here at all") is None
