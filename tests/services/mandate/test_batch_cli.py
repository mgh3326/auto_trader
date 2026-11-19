"""hk 3795 T3a: ``scripts/mandate_batch.py`` and the card text, without a database.

Preview versus commit, commit-before-send, the exit code of a failed send, the
operator-actor gate that runs before any database access, nonces that never reach
the output, and the runbook's commands against the real argparse. The database round
trips are ``test_batches_db.py`` (CI).
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.mandate import batch_cards as cards
from app.services.mandate import batch_rules as rules
from app.services.mandate import batches as bt
from scripts import mandate_batch as cli

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = ROOT / "docs/runbooks/mandate-batch-card.md"
DB = ["--database-url-env", "MANDATE_TEST_DB"]
ACTOR = ["--actor", "operator:cli:tester"]
NOW = datetime(2026, 11, 23, 0, 10, tzinfo=UTC)
NONCES = ["A" * 22, "B" * 22]


def facts(symbol: str = "005930", kind: str = "new_plan", card_no: int = 1):
    return cards.CardFacts(
        kind=kind,
        market="equity_kr",
        basket_key="basket-1",
        symbol=symbol,
        planned_amount=Decimal("2350000"),
        adopted_amount=Decimal("350000"),
        sector_key="chips",
        thesis="tight supply",
        invalidation_conditions=("capex cut",),
        terms_id=21,
        terms_sha256="a" * 64,
        plan_sha256="b" * 64,
        target_session=date(2026, 11, 23),
        approval_deadline_at=datetime(2026, 11, 23, 0, 0, tzinfo=UTC),
        expires_at=datetime(2026, 11, 23, 6, 30, tzinfo=UTC),
        card_no=card_no,
        open_order_count=3,
    )


class FakeSession:
    def __init__(self) -> None:
        self.log: list[str] = []

    async def commit(self):
        self.log.append("commit")

    async def rollback(self):
        self.log.append("rollback")


class Factory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def issued_batch() -> bt.IssuedBatch:
    batch = SimpleNamespace(
        id=9,
        realm="mock",
        basket_key="basket-1",
        market="equity_kr",
        target_session=date(2026, 11, 23),
        approval_deadline_at=datetime(2026, 11, 23, 0, 0, tzinfo=UTC),
        expires_at=datetime(2026, 11, 23, 6, 30, tzinfo=UTC),
    )
    members = [
        SimpleNamespace(
            id=100 + i, plan_id=10 + i, card_no=1, kind="new_plan", terms_id=21
        )
        for i in range(2)
    ]
    return bt.IssuedBatch(
        batch,
        tuple(bt.IssuedCard(m, n) for m, n in zip(members, NONCES, strict=True)),
        (bt.SkippedPlan(77, "000660", "account_ref_differs_from_live_terms"),),
    )


@pytest.fixture
def wired(monkeypatch):
    session = FakeSession()
    sent: list[tuple[str, dict | None]] = []

    async def create_batch(db, **kwargs):
        session.log.append("create")
        return issued_batch()

    async def card_facts(db, batch, member):
        return facts(symbol=f"S{member.plan_id}")

    async def armed(db, realm, basket_key):
        return (("005930", 2),)

    monkeypatch.setattr(bt, "create_batch", create_batch)
    monkeypatch.setattr(cli, "_card_facts", card_facts)
    monkeypatch.setattr(cli, "_armed_tranches", armed)

    async def sender(text, keyboard):
        session.log.append("send")
        sent.append((text, keyboard))
        return True, None

    return SimpleNamespace(session=session, sent=sent, sender=sender)


def parse(*argv: str):
    return cli._parser().parse_args([*DB, *argv])


async def run(world, *argv: str, sender="default"):
    args = parse(*argv)
    return await cli.run(
        args,
        session_factory=Factory(world.session),
        sender=world.sender if sender == "default" else sender,
        now=NOW,
        calendar=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_preview_runs_the_store_then_rolls_back_and_sends_nothing(wired) -> None:
    async def forbidden(text, keyboard):
        raise AssertionError("a preview sent a message")

    code, body = await run(
        wired,
        "create",
        "--realm",
        "mock",
        "--basket-key",
        "basket-1",
        *ACTOR,
        sender=forbidden,
    )
    assert code == cli.EXIT_OK and body["mode"] == "preview"
    assert wired.session.log == ["create", "rollback"]
    assert body["batch"]["id"] is None
    assert [c["symbol"] for c in body["cards"]] == ["S10", "S11"]
    assert body["skipped"][0]["reason"] == "account_ref_differs_from_live_terms"
    assert "sent" not in body


@pytest.mark.asyncio
async def test_commit_writes_first_and_sends_the_summary_then_one_card_each(
    wired,
) -> None:
    code, body = await run(
        wired,
        "create",
        "--realm",
        "mock",
        "--basket-key",
        "basket-1",
        *ACTOR,
        "--commit",
    )
    assert code == cli.EXIT_OK and body["mode"] == "commit"
    assert wired.session.log == ["create", "commit", "send", "send", "send"]
    assert body["batch"]["id"] == 9
    summary, first, second = wired.sent
    assert summary[1] is None and "오늘 적용 기준" in summary[0]
    assert "005930 2차" in summary[0] and "승인 대상 카드 2장" in summary[0]
    for (text, board), nonce, member_id in zip(
        (first, second), NONCES, (100, 101), strict=True
    ):
        buttons = board["inline_keyboard"][0]
        parsed = [rules.parse_callback_data(b["callback_data"]) for b in buttons]
        assert [p.action for p in parsed] == ["approve", "reject"]
        assert all(p.member_id == member_id and p.nonce == nonce for p in parsed)
        assert "해시" in text


@pytest.mark.asyncio
async def test_the_output_never_contains_a_nonce(wired) -> None:
    for extra in ([], ["--commit"]):
        _, body = await run(
            wired,
            "create",
            "--realm",
            "mock",
            "--basket-key",
            "basket-1",
            *ACTOR,
            *extra,
        )
        text = json.dumps(body)
        assert all(n not in text for n in NONCES)
        assert "nonce" not in text.lower()


@pytest.mark.asyncio
async def test_a_failed_send_keeps_the_rows_and_exits_2(wired) -> None:
    calls = {"n": 0}

    async def flaky(text, keyboard):
        calls["n"] += 1
        return (calls["n"] != 2), (
            None if calls["n"] != 2 else "telegram_transport_error"
        )

    code, body = await run(
        wired,
        "create",
        "--realm",
        "mock",
        "--basket-key",
        "basket-1",
        *ACTOR,
        "--commit",
        sender=flaky,
    )
    assert code == cli.EXIT_SEND_FAILED
    assert wired.session.log[:2] == ["create", "commit"]
    assert [r["sent"] for r in body["sent"]] == [True, False, True]  # others still sent
    assert body["sent"][1]["failure_code"] == "telegram_transport_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor", ["", "mgh", "system:mandate_projection", "operator:", "operator: "]
)
async def test_a_non_operator_actor_is_refused_before_any_database_access(
    actor,
) -> None:
    args = parse("create", "--realm", "mock", "--basket-key", "b", "--actor", actor)
    with pytest.raises(cli.MandateCliError):
        await cli.run(
            args
        )  # no session factory: a connection attempt would fail differently


@pytest.mark.asyncio
async def test_resend_preview_and_commit(wired, monkeypatch) -> None:
    async def get_batch(db, batch_id):
        return issued_batch().batch

    async def reissue(db, *, batch_id, actor, now):
        wired.session.log.append("reissue")
        return [
            bt.IssuedCard(
                SimpleNamespace(
                    id=200, plan_id=10, card_no=2, kind="new_plan", terms_id=21
                ),
                "C" * 22,
            )
        ]

    monkeypatch.setattr(bt, "get_batch", get_batch)
    monkeypatch.setattr(bt, "reissue_cards", reissue)
    code, body = await run(wired, "resend", "--batch-id", "9", *ACTOR)
    assert code == 0 and body["mode"] == "preview" and wired.sent == []
    assert wired.session.log == ["reissue", "rollback"]
    wired.session.log.clear()
    code, body = await run(wired, "resend", "--batch-id", "9", *ACTOR, "--commit")
    assert code == 0 and wired.session.log == ["reissue", "commit", "send"]
    (text, board) = wired.sent[0]
    parsed = rules.parse_callback_data(board["inline_keyboard"][0][0]["callback_data"])
    assert parsed.member_id == 200 and parsed.nonce == "C" * 22
    assert "C" * 22 not in json.dumps(body)


# ------------------------------------------------------------------- the card text
def test_the_card_shows_what_is_approved_and_what_a_rejection_does() -> None:
    text = cards.render_card(facts())
    for needle in (
        "신규 계획 승인",
        "005930",
        "2350000",
        "350000",
        "2000000",  # new purchase = planned - adopted
        "a" * 64,
        "b" * 64,
        "tight supply",
        "capex cut",
        "대상 거래일 2026-11-23",
        "승인 마감 11-23 09:00",
        "배치 만료 11-23 15:30",
    ):
        assert needle in text
    assert "rejected" in text


def test_a_reapproval_card_states_the_open_order_count_a_rejection_leaves() -> None:
    text = cards.render_card(facts(kind="reapproval"))
    assert "위임장 변경 재승인" in text
    assert "미결 제안·주문 3건" in text and "폐기" in text


def test_a_summary_without_members_is_the_whole_message() -> None:
    text = cards.render_summary(
        cards.SummaryFacts(
            "equity_kr",
            "b",
            date(2026, 11, 23),
            datetime(2026, 11, 23, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 23, 6, 30, tzinfo=UTC),
        )
    )
    assert "승인할 변경이 없습니다" in text and "armed 회차 없음" in text


def test_text_stays_inside_the_telegram_limit() -> None:
    long = facts()
    long = cards.CardFacts(**{**long.__dict__, "thesis": "x" * 9000})
    assert len(cards.render_card(long)) <= cards.MAX_TEXT
    assert len(cards.render_answered(long, "승인했습니다.")) <= cards.MAX_TEXT


# ---------------------------------------------------------------------- the runbook
def _runbook_commands() -> list[list[str]]:
    text = RUNBOOK.read_text()
    out = []
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("uv run python -m scripts.mandate_batch "):
                out.append(shlex.split(line)[5:])
    return out


def test_every_runbook_command_parses_with_the_real_argparse() -> None:
    commands = _runbook_commands()
    assert {c[2] for c in commands} == {"create", "resend", "status"}
    for argv in commands:
        cli._parser().parse_args(argv)
