"""hk 5441 — ``scripts/execution_mandates.py`` (design §4.3, D2, D9, D12, D17, S6, S7).

The commands are run against a recording session: what they add, and whether
they commit or roll back, is the behaviour under test. The DB rules behind them
run in ``tests/services/execution_intent_gateway/test_upbit_db.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.schemas.execution_intent import ExecutionIntentV2
from app.services.execution_intent_gateway import mandates
from app.services.execution_intent_gateway.mandates import MandateError
from scripts import execution_mandates as cli

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 20, 5, 20, tzinfo=UTC)
ACCOUNT_REF = "upbit:upbit:" + "b" * 64
SHA = "a" * 40


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.committed = 0
        self.rolled_back = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


@pytest.fixture
def sleeve(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A go-path upbit sleeve in ``shadow`` with no history and no mandate."""
    state = SimpleNamespace(
        row=SimpleNamespace(
            id=7,
            sleeve_key="upbit-trend",
            execution_path="go",
            account_mode="upbit",
            market="crypto",
        ),
        version=SimpleNamespace(
            id=70,
            version=1,
            state="shadow",
            budget_cap=Decimal("1950000"),
            loss_limit=Decimal("1000000"),
        ),
        has_history=False,
        unrevoked=None,
    )

    async def get_sleeve_by_key(_session, _key):
        return state.row

    async def current_version(_session, _sleeve_id):
        return state.version

    async def no_lock(_session, _sleeve_id):
        return None

    async def has_history(_session, _sleeve_id):
        return state.has_history

    async def unrevoked(_session, _sleeve_id):
        return state.unrevoked

    monkeypatch.setattr(mandates, "get_sleeve_by_key", get_sleeve_by_key)
    monkeypatch.setattr(mandates, "current_version", current_version)
    monkeypatch.setattr(mandates, "acquire_sleeve_lock", no_lock)
    monkeypatch.setattr(mandates, "_sleeve_has_history", has_history)
    monkeypatch.setattr(mandates, "_unrevoked_mandate", unrevoked)
    return state


def _propose(*extra: str, mode: str = "shadow", **overrides: str) -> list[str]:
    values = {
        "--sleeve-key": "upbit-trend",
        "--strategy": "upbit-trend-long-v1",
        "--strategy-code-sha": SHA,
        "--per-intent-max-notional-krw": "480000",
        "--max-open-intents": "4",
        "--price-band-bps": "30",
        "--max-intent-ttl-seconds": "43200",
        "--expires-at": (NOW + timedelta(days=91)).isoformat(),
        "--actor": "robin",
        **overrides,
    }
    argv = ["--database-url-env", "DB", "propose", "--mode", mode]
    for symbol in ("KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP"):
        argv += ["--symbol", symbol]
    for flag, value in values.items():
        argv += [flag, value]
    return [*argv, *extra]


async def _run(argv: list[str], session: _Session, **kwargs: Any) -> Any:
    args = cli._parser().parse_args(argv)

    async def balances() -> list[dict[str, str]]:
        return [
            {
                "currency": "KRW",
                "balance": "1000000",
                "locked": "0",
                "avg_buy_price": "0",
            }
        ]

    _code, payload = await cli.run(
        args,
        session_factory=lambda: session,
        now=NOW,
        account_ref=kwargs.pop("account_ref", lambda: ACCOUNT_REF),
        balance_reader=balances,
        **kwargs,
    )
    return payload


@pytest.mark.asyncio
async def test_propose_previews_by_default_and_writes_nothing(sleeve) -> None:
    session = _Session()
    payload = await _run(_propose(), session)
    assert payload["mode"] == "preview"
    assert len(payload["mandate"]["content_sha256"]) == 64
    assert session.added == [] and session.committed == 0 and session.rolled_back == 1


@pytest.mark.asyncio
async def test_d12_a_shadow_mandate_is_created_by_commit_alone(sleeve) -> None:
    session = _Session()
    payload = await _run(_propose("--commit"), session)
    [row] = session.added
    assert payload["mode"] == "commit" and session.committed == 1
    assert row.mode == "shadow" and row.account_ref is None
    assert row.approval_ref == "operator-cli/robin"
    assert sorted(row.allowed_symbols) == ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP"]
    assert row.loss_sell_mode == "human_card"


@pytest.mark.asyncio
async def test_the_content_hash_changes_with_any_term(sleeve) -> None:
    first = await _run(_propose(), _Session())
    other = await _run(_propose(**{"--price-band-bps": "29"}), _Session())
    assert first["mandate"]["content_sha256"] != other["mandate"]["content_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"--per-intent-max-notional-krw": "480001"},
        {"--per-intent-max-notional-krw": "4999"},
        {"--max-open-intents": "5"},
        {"--price-band-bps": "31"},
        {"--max-intent-ttl-seconds": "43201"},
        {"--expires-at": (NOW + timedelta(days=91, seconds=1)).isoformat()},
        {"--expires-at": NOW.isoformat()},
        {"--strategy-code-sha": "A" * 40},
        {"--actor": "system:strategy-job"},
    ],
)
async def test_d9_a_mandate_outside_the_decided_ceilings_is_refused(
    sleeve, overrides
) -> None:
    session = _Session()
    with pytest.raises(MandateError):
        await _run(_propose("--commit", **overrides), session)
    assert session.added == [] and session.committed == 0


@pytest.mark.asyncio
async def test_s7_a_card_sleeve_takes_no_mandate(sleeve) -> None:
    sleeve.row.execution_path = "card"
    with pytest.raises(MandateError, match="go path"):
        await _run(_propose("--commit"), _Session())


@pytest.mark.asyncio
async def test_s7_a_sleeve_with_orders_or_a_live_mandate_takes_no_new_mandate(
    sleeve,
) -> None:
    sleeve.has_history = True
    with pytest.raises(MandateError, match="already has orders"):
        await _run(_propose("--commit"), _Session())
    sleeve.has_history = False
    sleeve.unrevoked = SimpleNamespace(id=uuid4())
    with pytest.raises(MandateError, match="not revoked"):
        await _run(_propose("--commit"), _Session())


@pytest.mark.asyncio
async def test_the_sleeve_state_must_match_the_mandate_mode(sleeve) -> None:
    with pytest.raises(MandateError, match="needs the sleeve in state"):
        await _run(_propose(mode="live"), _Session())


@pytest.mark.asyncio
async def test_d17_a_live_preview_shows_the_fingerprint_and_the_balances(
    sleeve,
) -> None:
    sleeve.version.state = "live"
    payload = await _run(_propose(mode="live"), _Session())
    assert payload["mandate"]["account_ref"] == ACCOUNT_REF
    assert payload["account_summary"][0]["currency"] == "KRW"


@pytest.mark.asyncio
async def test_b12_no_fingerprint_no_live_mandate(sleeve) -> None:
    sleeve.version.state = "live"
    with pytest.raises(MandateError, match="fingerprint"):
        await _run(_propose(mode="live"), _Session(), account_ref=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confirmations",
    [
        [],
        ["--approval-ref", "decision/2026-11-21/at-5438-live-mandate"],
        [
            "--approval-ref",
            "decision/2026-11-21/at-5438-live-mandate",
            "--confirm-content-sha256",
            "0" * 64,
            "--confirm-account-ref",
            ACCOUNT_REF,
        ],
        [
            "--approval-ref",
            "decision/2026-11-21/at-5438-live-mandate",
            "--confirm-content-sha256",
            "<sha>",
            "--confirm-account-ref",
            "upbit:upbit:" + "c" * 64,
        ],
        [
            "--approval-ref",
            "chat-ok",
            "--confirm-content-sha256",
            "<sha>",
            "--confirm-account-ref",
            ACCOUNT_REF,
        ],
    ],
)
async def test_d2_a_live_mandate_needs_every_confirmation(
    sleeve, confirmations
) -> None:
    sleeve.version.state = "live"
    sha = (await _run(_propose(mode="live"), _Session()))["mandate"]["content_sha256"]
    argv = [sha if item == "<sha>" else item for item in confirmations]
    session = _Session()
    with pytest.raises(MandateError):
        await _run(_propose("--commit", *argv, mode="live"), session)
    assert session.added == [] and session.committed == 0


@pytest.mark.asyncio
async def test_d2_a_live_mandate_is_created_when_all_three_are_repeated(sleeve) -> None:
    sleeve.version.state = "live"
    sha = (await _run(_propose(mode="live"), _Session()))["mandate"]["content_sha256"]
    session = _Session()
    await _run(
        _propose(
            "--commit",
            "--approval-ref",
            "decision/2026-11-21/at-5438-live-mandate",
            "--confirm-content-sha256",
            sha,
            "--confirm-account-ref",
            ACCOUNT_REF,
            mode="live",
        ),
        session,
    )
    [row] = session.added
    assert row.mode == "live" and row.account_ref == ACCOUNT_REF
    assert row.approval_ref == "decision/2026-11-21/at-5438-live-mandate"


def test_no_amount_has_a_default() -> None:
    parser = cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--database-url-env",
                "DB",
                "propose",
                "--sleeve-key",
                "s",
                "--mode",
                "shadow",
            ]
        )
    args = parser.parse_args(_propose())
    assert args.commit is False


@pytest.mark.parametrize(
    "argv",
    [
        ["revoke", "--mandate-id", "x", "--reason", "r", "--actor", "robin"],
        [
            "exit",
            "--mandate",
            "x",
            "--symbol",
            "KRW-XRP",
            "--max-quantity",
            "1",
            "--limit-price-krw",
            "3000",
            "--max-slippage-bps",
            "30",
            "--reason",
            "r",
            "--actor",
            "robin",
        ],
        [
            "control",
            "--account",
            "upbit_live",
            "--mode",
            "ACTIVE",
            "--reason",
            "r",
            "--actor",
            "robin",
        ],
    ],
)
def test_every_writing_command_previews_unless_commit(argv: list[str]) -> None:
    parser = cli._parser()
    assert parser.parse_args(["--database-url-env", "DB", *argv]).commit is False
    assert (
        parser.parse_args(["--database-url-env", "DB", *argv, "--commit"]).commit
        is True
    )


def test_control_names_only_the_upbit_account_and_the_closed_modes() -> None:
    parser = cli._parser()
    base = ["--database-url-env", "DB", "control", "--reason", "r", "--actor", "robin"]
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--account", "kis_mock", "--mode", "ACTIVE"])
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--account", "upbit_live", "--mode", "OPEN"])


def test_s6_the_exit_intent_is_a_sell_bound_to_its_exit_row() -> None:
    exit_row = SimpleNamespace(
        id=uuid4(),
        symbol="KRW-XRP",
        max_quantity=Decimal("12.50000000"),
        expires_at=NOW + timedelta(hours=1),
    )
    mandate = SimpleNamespace(
        id=uuid4(),
        strategy="upbit-trend-long-v1",
        mode="live",
        sleeve_id=7,
        strategy_code_sha=SHA,
        max_intent_ttl=timedelta(hours=12),
    )
    args = SimpleNamespace(limit_price_krw="3000", max_slippage_bps="30")

    intent = cli._exit_intent(exit_row, mandate, args, now=NOW)

    assert isinstance(intent, ExecutionIntentV2)
    assert (intent.side, intent.origin) == ("sell", "operator_exit")
    assert intent.operator_exit_id == exit_row.id
    assert intent.authorization_ref == f"operator-exit:{exit_row.id}"
    assert intent.max_quantity_decimal == "12.5"
    assert intent.expires_at == exit_row.expires_at
    # One exit row, one intent id, however often the command is run.
    assert (
        cli._exit_intent(exit_row, mandate, args, now=NOW).intent_id == intent.intent_id
    )


def test_s6_the_exit_intent_lives_no_longer_than_the_mandate_lets_an_intent_live() -> (
    None
):
    """PR 2486 review: a 15-minute mandate must still get a usable exit intent.

    The exit row lasts an hour. Copying that into the intent made every exit of
    a short-TTL mandate ``mandate_scope_mismatch`` after its row was committed.
    """
    from app.services.execution_intent_gateway.upbit_decisions import (
        evaluate_operator_exit,
    )
    from tests.services.execution_intent_gateway import upbit_support as support

    ttl = timedelta(minutes=15)
    exit_row = SimpleNamespace(
        id=uuid4(),
        symbol=support.SYMBOL,
        max_quantity=Decimal("10"),
        expires_at=support.NOW + timedelta(hours=1),
    )
    the_mandate = support.mandate(max_intent_ttl=ttl, revoked=True)
    row = SimpleNamespace(
        id=the_mandate.mandate_id,
        strategy=the_mandate.strategy,
        mode=the_mandate.mode,
        sleeve_id=the_mandate.sleeve_id,
        strategy_code_sha=the_mandate.strategy_code_sha,
        max_intent_ttl=ttl,
    )
    args = SimpleNamespace(limit_price_krw="3000", max_slippage_bps="30")

    intent = cli._exit_intent(exit_row, row, args, now=support.NOW)

    assert intent.expires_at == support.NOW + ttl
    snapshot = support.sell_intent(
        intent_id=intent.intent_id,
        origin="operator_exit",
        operator_exit_id=exit_row.id,
        expires_at=intent.expires_at,
        max_quantity_decimal=Decimal(intent.max_quantity_decimal),
        created_at=support.NOW,
    )
    refusal = evaluate_operator_exit(
        snapshot,
        quantity=Decimal("10"),
        facts=support.facts(
            mandate=the_mandate,
            operator_exit=support.operator_exit(
                snapshot, max_quantity=Decimal("10"), expires_at=exit_row.expires_at
            ),
            symbol=support.symbol_view(holdings=Decimal("10")),
        ),
        reads=support.reads(),
    )
    assert refusal is None
    # A longer mandate TTL leaves the exit row's own deadline in charge.
    row.max_intent_ttl = timedelta(hours=12)
    assert (
        cli._exit_intent(exit_row, row, args, now=support.NOW).expires_at
        == exit_row.expires_at
    )


class _ExitSession(_Session):
    """A session whose rows expire at commit, as the real one's do."""

    def __init__(self, mandate_row: Any) -> None:
        super().__init__()
        self._mandate_row = mandate_row

    async def get(self, _model: Any, _key: Any) -> Any:
        return self._mandate_row


class _ExpiringRow:
    """Stands in for an ORM row: reading it after the commit is an error."""

    def __init__(self, session: _Session, **values: Any) -> None:
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name: str) -> Any:
        if self._session.committed:
            raise AssertionError(f"{name} was read from a row the commit expired")
        return self._values[name]


@pytest.mark.asyncio
async def test_s6_a_committed_exit_intent_starts_its_lifetime_when_it_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 2486 review (65wMjR): the mandate lets an intent live one second. The
    command started at NOW; committing the exit row, reading the kill row and
    waiting for the store's locks took three seconds. An expiry of NOW + 1s is
    over before the INSERT and the operator is left with an exit row and no
    intent. The intent is built by the store, from the clock it reads under its
    locks, and the command reports the expiry that was stored."""
    from app.services.kis_mock_runner.control import KillMode

    ttl = timedelta(seconds=1)
    stored_at = NOW + timedelta(seconds=3)
    mandate_id = uuid4()
    session = _ExitSession(None)
    exit_row = _ExpiringRow(
        session,
        id=uuid4(),
        symbol="KRW-XRP",
        max_quantity=Decimal("10"),
        expires_at=NOW + timedelta(hours=1),
    )
    session._mandate_row = _ExpiringRow(
        session,
        id=mandate_id,
        strategy="upbit-trend-long-v1",
        mode="live",
        sleeve_id=7,
        strategy_code_sha=SHA,
        max_intent_ttl=ttl,
    )
    calls: list[dict[str, Any]] = []

    async def create_operator_exit(_session: Any, **_kwargs: Any) -> Any:
        return exit_row

    async def kill_row(_store: Any) -> Any:
        return SimpleNamespace(mode=KillMode.ACTIVE)

    class _Store:
        def __init__(self, _factory: Any) -> None:
            pass

        async def store_intent_for_operator_exit(
            self, build: Any, **kwargs: Any
        ) -> Any:
            calls.append(kwargs)
            return build(stored_at)

    monkeypatch.setattr(cli, "create_operator_exit", create_operator_exit)
    monkeypatch.setattr(cli, "read_effective_kill_switch", kill_row)
    monkeypatch.setattr(cli, "PostgresAccountControlStore", lambda *_a: None)
    monkeypatch.setattr(cli, "UpbitGatewayStore", _Store)

    def args(commit: bool) -> SimpleNamespace:
        return SimpleNamespace(
            mandate=str(mandate_id),
            symbol="KRW-XRP",
            max_quantity="10",
            reason="r",
            actor="robin",
            limit_price_krw="3000",
            max_slippage_bps="30",
            commit=commit,
        )

    preview = await cli._exit(
        session,  # type: ignore[arg-type]
        args(False),
        now=NOW,
        session_factory=None,
        environment={},
    )
    assert calls == [] and session.rolled_back == 1
    # The preview shows what a store at the time of the preview would give.
    assert ExecutionIntentV2.model_validate(preview["intent"]).expires_at == NOW + ttl

    committed = await cli._exit(
        session,  # type: ignore[arg-type]
        args(True),
        now=NOW,
        session_factory=None,
        environment={},
    )

    assert session.committed == 1
    assert committed["intent_stored"] is True
    # Stored at NOW + 3s with one second to live: not NOW + 1s, which is past.
    stored = ExecutionIntentV2.model_validate(committed["intent"])
    assert stored.expires_at == stored_at + ttl
    assert stored.expires_at > stored_at
    assert calls == [
        {
            "sleeve_id": 7,
            "mandate_id": mandate_id,
            "reads": calls[0]["reads"],
        }
    ]
    assert calls[0]["reads"].kill_mode is KillMode.ACTIVE


def test_the_database_url_is_never_echoed(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("DB_FOR_TEST", "not-a-url-with-secret-value")
    code = cli.main(["--database-url-env", "DB_FOR_TEST", "list"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert "secret-value" not in captured.err + captured.out
