from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.account_value.identity import configured_fingerprint
from app.services.account_value.readers import BrokerReader, kis_live_identity
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.upbit import client as upbit_client
from scripts import account_value_snapshot
from tests.services.account_value.test_cli import SessionContext
from tests.services.account_value.test_readers import _fake_kis_client, _read_kis
from tests.services.account_value.test_snapshot import Session

INTERNAL_KEY = "toss:toss_live:" + "b" * 64


class FakeToss:
    async def accounts(self):
        return [SimpleNamespace(account_seq=7)]

    async def holdings(self):
        return SimpleNamespace(items=[])

    async def aclose(self):
        pass


async def _snapshot_account(broker, monkeypatch):
    """Account key the snapshot writer stores for ``broker``, read end to end."""
    if broker == "kis":
        monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
        monkeypatch.setattr(settings, "kis_app_key", "app-key")
        monkeypatch.setattr(settings, "kis_app_secret", "app-secret")
        client = _fake_kis_client(
            rows=[{"pdno": "005930", "hldg_qty": "2"}], complete=True
        )
        return (await _read_kis(monkeypatch, "kr", client)).account
    if broker == "toss":
        monkeypatch.setattr(settings, "toss_api_account_seq", 7)
        monkeypatch.setattr(settings, "toss_api_client_secret", None)
        monkeypatch.setattr(
            TossReadClient, "from_settings", lambda *args, **kwargs: FakeToss()
        )
        reader = BrokerReader(None, "toss", "kr")
    else:
        monkeypatch.setattr(settings, "upbit_access_key", "access")
        monkeypatch.setattr(settings, "upbit_secret_key", "secret")

        async def fetch_my_coins():
            return []

        monkeypatch.setattr(upbit_client, "fetch_my_coins", fetch_my_coins)
        reader = BrokerReader(None, "upbit", "crypto")

    async def marks(*args):
        return {}

    monkeypatch.setattr(reader, "_marks", marks)
    return (await reader.read())[0].account


def _recording(monkeypatch):
    session = Session()
    monkeypatch.setattr(
        account_value_snapshot, "session_factory", lambda: SessionContext(session)
    )
    seen = []

    async def record(session, account, start, end, confirmed_by, reason, *, commit):
        seen.append(account)
        return "preview"

    monkeypatch.setattr(account_value_snapshot, "record_confirmation", record)
    return session, seen


def _run_main(monkeypatch, tail):
    monkeypatch.setattr(
        "sys.argv",
        ["account_value_snapshot.py", "--confirm-flows", *tail[:1]]
        + ["2026-01-01", "2026-01-08", "operator", "checked", *tail[1:]],
    )
    with pytest.raises(SystemExit) as info:
        account_value_snapshot.main()
    assert info.value.code == 1


def _confirm_args(account, *extra):
    return account_value_snapshot.parser().parse_args(
        [
            "--confirm-flows",
            account,
            "2026-01-01",
            "2026-01-08",
            "operator",
            "checked",
            *extra,
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["kis", "toss", "upbit"])
async def test_broker_name_records_the_key_the_snapshot_writer_uses(
    monkeypatch, capsys, broker
):
    expected = await _snapshot_account(broker, monkeypatch)
    assert expected.startswith(f"{broker}:") and not expected.endswith(
        ":identity_unknown"
    )
    _, seen = _recording(monkeypatch)
    await account_value_snapshot.run(_confirm_args(broker))
    assert seen == [expected]
    out = capsys.readouterr().out
    assert f'"account": "{broker}:' in out and out.count(":***") == 1
    assert expected.rsplit(":", 1)[1] not in out


@pytest.mark.asyncio
async def test_internal_key_is_passed_through_unchanged(monkeypatch, capsys):
    _, seen = _recording(monkeypatch)

    async def must_not_resolve(broker):
        raise AssertionError("internal key must not reach the broker resolver")

    monkeypatch.setattr(account_value_snapshot, "resolve_account_key", must_not_resolve)
    await account_value_snapshot.run(_confirm_args(INTERNAL_KEY))
    assert seen == [INTERNAL_KEY]
    assert "b" * 64 not in capsys.readouterr().out


@pytest.mark.parametrize("extra", [[], ["--commit"]])
def test_unknown_account_fails_before_any_write(monkeypatch, capsys, extra):
    session, seen = _recording(monkeypatch)
    _run_main(monkeypatch, ["12345678-01", *extra])
    assert seen == [] and session.commits == 0
    captured = capsys.readouterr()
    assert "ACCOUNT" in captured.err
    assert "12345678-01" not in captured.out + captured.err


def test_unresolvable_broker_identity_fails_before_any_write(monkeypatch, capsys):
    monkeypatch.setattr(settings, "kis_account_no", None)
    session, seen = _recording(monkeypatch)
    _run_main(monkeypatch, ["kis", "--commit"])
    assert seen == [] and session.commits == 0
    assert "kis 계좌 식별자를 확인할 수 없습니다" in capsys.readouterr().err


def test_fingerprint_golden_vectors():
    """Pin the exact bytes the account-key fingerprint is computed from.

    The identifiers mirror what each broker identity feeds
    ``configured_fingerprint`` (KIS: cano+product+app_key; Toss: account seq;
    Upbit: access key). A silent change to those inputs starts a separate
    series (contract: "지문이 바뀌면 별개 시리즈다"), so the digests below are
    computed once from the derivation and must not drift.
    """
    assert (
        configured_fingerprint(
            "kis", "kis_live", "12345678" + "01" + "app-key", "app-secret"
        )
        == "kis:kis_live:19c87d5dba078a0f83bab7d3fcdf3683039979c9dc485756a556b27342b8a283"
    )
    assert (
        configured_fingerprint("toss", "toss_live", "7", "secret")
        == "toss:toss_live:a512e3a15e24839a18c7169142e311a242455d04fe8915b333b3d1cab9b70981"
    )
    assert (
        configured_fingerprint("upbit", "upbit", "access", "secret")
        == "upbit:upbit:3be0842ef6dd337b69e71d3d7144c6eb11b49a3ddc387a32fa4e5f5d9994e9e6"
    )


@pytest.mark.parametrize("app_key", ["", "   "])
def test_empty_kis_app_key_yields_no_account_key(monkeypatch, capsys, app_key):
    monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
    monkeypatch.setattr(settings, "kis_app_key", app_key)
    monkeypatch.setattr(settings, "kis_app_secret", "app-secret")
    assert kis_live_identity() is None
    session, seen = _recording(monkeypatch)
    _run_main(monkeypatch, ["kis", "--commit"])
    assert seen == [] and session.commits == 0
    assert "kis 계좌 식별자를 확인할 수 없습니다" in capsys.readouterr().err


def test_configured_kis_key_matches_golden_vector(monkeypatch):
    monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
    monkeypatch.setattr(settings, "kis_app_key", "app-key")
    monkeypatch.setattr(settings, "kis_app_secret", "app-secret")
    assert (
        kis_live_identity()
        == "kis:kis_live:19c87d5dba078a0f83bab7d3fcdf3683039979c9dc485756a556b27342b8a283"
    )


def test_missing_toss_secret_is_account_error_without_exception_text(
    monkeypatch, capsys
):
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_api_client_id", "test-client-id")
    monkeypatch.setattr(settings, "toss_api_client_secret", None)
    monkeypatch.setattr(settings, "toss_api_account_seq", 7)
    session, seen = _recording(monkeypatch)
    _run_main(monkeypatch, ["toss", "--commit"])
    assert seen == [] and session.commits == 0
    captured = capsys.readouterr()
    assert "toss 계좌 식별자를 확인할 수 없습니다" in captured.err
    assert "TossMissingCredentials" not in captured.out + captured.err


@pytest.mark.parametrize(
    "dates", [["2026-02-30", "2026-03-01"], ["2026-03-01", "2026-02-30"]]
)
def test_invalid_dates_fail_before_account_resolution(monkeypatch, capsys, dates):
    calls = []

    async def counting_resolve(broker):
        calls.append(broker)
        return INTERNAL_KEY

    monkeypatch.setattr(account_value_snapshot, "resolve_account_key", counting_resolve)
    session, seen = _recording(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        [
            "account_value_snapshot.py",
            "--confirm-flows",
            "toss",
            *dates,
            "operator",
            "checked",
        ],
    )
    with pytest.raises(SystemExit) as info:
        account_value_snapshot.main()
    assert info.value.code != 0
    assert calls == [] and seen == [] and session.commits == 0
