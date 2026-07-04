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
