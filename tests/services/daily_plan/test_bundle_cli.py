"""The manual CLI: read-only by default, one store call on commit."""

from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.account_value import readers
from app.services.daily_plan.bundle import bundle_sha256
from app.services.mandate.account_ref import derive_account_ref
from scripts import build_daily_plan_bundle as cli
from tests.services.daily_plan import bundle_fixtures as f

NOW = f.at(f.DAY, 8, 25)
BUNDLE_ID = UUID("11111111-2222-3333-4444-555555555555")
SECRET = "Test-Only-Secret-Key-0123456789abcdef"


def config(**changes):
    values = {
        "SECRET_KEY": SECRET,
        "kis_account_no": "12345678-01",
        "kis_app_key": "app-key",
        "kis_app_secret": "app-secret",
        "toss_api_account_seq": 7,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class Session:
    def __init__(self, log):
        self.statements = []
        log.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, statement, *_):
        self.statements.append(str(statement))


@pytest.fixture
def harness(monkeypatch):
    sessions, stored, assembled = [], [], []

    async def assemble(db, **kwargs):
        assembled.append((db, list(db.statements), kwargs))
        return f.build()

    async def store(db, *, day, body, claimed_sha256):
        stored.append((db, day, body, claimed_sha256))
        return BUNDLE_ID

    monkeypatch.setattr(cli, "assemble_bundle", assemble)
    monkeypatch.setattr(cli, "store_input_bundle", store)
    monkeypatch.setattr(cli, "session_facts", lambda _: f.facts())

    async def run(*argv, now=NOW):
        return await cli.run(
            cli.parse_args(list(argv)),
            session_factory=lambda: Session(sessions),
            config=config(),
            now=now,
        )

    return SimpleNamespace(
        run=run, sessions=sessions, stored=stored, assembled=assembled
    )


@pytest.mark.parametrize("extra", [[], ["--dry-run"]], ids=["default", "flag"])
@pytest.mark.asyncio
async def test_dry_run_reads_in_a_read_only_transaction_and_writes_nothing(
    harness, extra
):
    code, report = await harness.run("--date", "2026-11-27", *extra)
    assert code == 0 and report["mode"] == "dry_run" and report["status"] == "built"
    assert not harness.stored
    [session] = harness.sessions
    assert session.statements[0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    [(db, before, kwargs)] = harness.assembled
    assert db is session and before == session.statements
    assert kwargs["closed_at"] == f.CLOSED_AT
    assert kwargs["cutoff_at"] == f.CUTOFF_AT
    assert report["bundle_sha256"] == bundle_sha256(f.build())
    assert report["candidates"] == 2
    assert report["tallies"]["news"] == {"direction_unclassified": 2}
    assert report["tallies"]["cash"] == {"input_missing": 1, "present": 1}


@pytest.mark.asyncio
async def test_commit_stores_once_in_its_own_transaction(harness):
    code, report = await harness.run("--date", "2026-11-27", "--commit")
    assert (code, report["status"], report["bundle_id"]) == (
        0,
        "stored",
        str(BUNDLE_ID),
    )
    read, write = harness.sessions
    assert "READ ONLY" in read.statements[0]
    [(db, day, body, digest)] = harness.stored
    assert db is write and day == f.DAY
    assert digest == bundle_sha256(body) == report["bundle_sha256"]
    assert not write.statements


@pytest.mark.asyncio
async def test_cutoff_can_be_earlier_but_not_after_the_premarket_cutoff(harness):
    code, _ = await harness.run("--date", "2026-11-27", "--cutoff", "08:30", now=f.LATE)
    assert code == 0
    assert harness.assembled[-1][2]["closed_at"] == f.CUTOFF_AT
    code, report = await harness.run(
        "--date", "2026-11-27", "--cutoff", "08:31", now=f.LATE
    )
    assert (code, report["reason"]) == (2, "cutoff_after_premarket")
    code, report = await harness.run(
        "--date", "2026-11-27", "--cutoff", "08:20+00:00", now=f.LATE
    )
    assert (code, report["reason"]) == (2, "invalid_cutoff")


@pytest.mark.parametrize(
    ("argv", "now", "reason"),
    [
        (["--date", "2026-11-27"], f.at(f.DAY, 8, 19), "cutoff_not_reached"),
        (
            ["--date", "2026-11-27", "--commit"],
            f.at(date(2026, 11, 28), 8, 25),
            "commit_date_not_today",
        ),
    ],
)
@pytest.mark.asyncio
async def test_refusals_open_no_session(harness, argv, now, reason):
    code, report = await harness.run(*argv, now=now)
    assert (code, report["status"], report["reason"]) == (2, "refused", reason)
    assert not harness.sessions and not harness.stored


@pytest.mark.asyncio
async def test_dry_run_of_a_past_day_is_allowed(harness):
    code, _ = await harness.run(
        "--date", "2026-11-27", now=datetime(2026, 12, 1, tzinfo=f.KST)
    )
    assert code == 0 and not harness.stored


@pytest.mark.asyncio
async def test_non_session_day_is_a_clean_skip():
    # Saturday, answered by the real exchange calendar.
    def no_session():
        raise AssertionError("no database session for a non-session day")

    code, report = await cli.run(
        cli.parse_args(["--date", "2026-11-28", "--commit"]),
        session_factory=no_session,
        config=config(),
        now=f.at(date(2026, 11, 28), 8, 25),
    )
    assert (code, report["status"], report["reason"]) == (
        0,
        "skipped",
        "not_a_session",
    )


def test_dry_run_and_commit_are_exclusive():
    with pytest.raises(SystemExit):
        cli.parse_args(["--date", "2026-11-27", "--dry-run", "--commit"])
    with pytest.raises(SystemExit):
        cli.parse_args([])


@pytest.mark.asyncio
async def test_bindings_match_the_account_value_writer_and_the_order_path(
    monkeypatch,
):
    from app.core.config import settings

    current = config()
    for name, value in vars(current).items():
        monkeypatch.setattr(settings, name, value)

    class View:
        async def accounts(self):
            return [SimpleNamespace(account_seq=7)]

    bindings = cli.resolve_account_bindings(current)
    kis = readers.kis_live_identity()
    toss = await readers.toss_live_identity(View())
    assert set(bindings) == {kis, toss}
    assert bindings[kis].account_mode == "kis_live"
    assert bindings[kis].account_ref == derive_account_ref(
        "kis_live", "12345678-01", secret=SECRET
    )
    assert bindings[toss].account_ref == derive_account_ref(
        "toss_live", "7", secret=SECRET
    )


@pytest.mark.parametrize(
    ("changes", "modes"),
    [
        ({"kis_account_no": None}, ["toss_live"]),
        ({"kis_account_no": "123"}, ["toss_live"]),
        ({"kis_app_key": " "}, ["toss_live"]),
        ({"toss_api_account_seq": None}, ["kis_live"]),
        ({"SECRET_KEY": ""}, []),
    ],
)
def test_unconfigured_accounts_get_no_binding(changes, modes):
    bindings = cli.resolve_account_bindings(config(**changes))
    assert sorted(item.account_mode for item in bindings.values()) == modes
