from __future__ import annotations

import pytest

from scripts import toss_live_smoke


def test_main_without_preflight_exits_zero(capsys) -> None:
    code = toss_live_smoke.main([])

    assert code == 0
    assert "disabled" in capsys.readouterr().out


def test_main_disabled_env_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TOSS_API_ENABLED", raising=False)

    code = toss_live_smoke.main(["--preflight"])

    assert code == 0
    assert "TOSS_API_ENABLED" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_preflight_redacts_secret(monkeypatch, capsys) -> None:
    class FakeClient:
        async def accounts(self):
            return [type("Account", (), {"account_seq": 1})()]

        async def holdings(self):
            return type("Holdings", (), {"items": []})()

        async def prices(self, symbols):
            assert symbols == ["005930"]
            return []

        async def aclose(self):
            return None

    monkeypatch.setattr(
        toss_live_smoke.TossReadClient, "from_settings", lambda: FakeClient()
    )

    code = await toss_live_smoke.run_preflight(["005930"])

    assert code == 0
    output = capsys.readouterr().out
    assert "client_secret" not in output.lower()


def test_order_test_requires_explicit_order_arguments(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")

    code = toss_live_smoke.main(["--order-test", "--symbol", "005930"])

    assert code == 2
    output = capsys.readouterr().out
    assert "--market is required for --order-test" in output
    assert "--quantity is required for --order-test" in output
    assert "--price is required for --order-test" in output


def test_confirm_requires_explicit_order_arguments(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")
    monkeypatch.setenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", "true")

    code = toss_live_smoke.main(["--confirm", "--symbol", "005930"])

    assert code == 2
    output = capsys.readouterr().out
    assert "--market is required for --confirm" in output
    assert "--quantity is required for --confirm" in output
    assert "--price is required for --confirm" in output


def test_order_test_disabled_when_toss_api_disabled(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TOSS_API_ENABLED", raising=False)

    code = toss_live_smoke.main(
        [
            "--order-test",
            "--market",
            "kr",
            "--symbol",
            "005930",
            "--quantity",
            "1",
            "--price",
            "50000",
        ]
    )

    assert code == 0
    assert "TOSS_API_ENABLED is not truthy" in capsys.readouterr().out


def test_confirm_disabled_without_mutation_gate(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")
    monkeypatch.delenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", raising=False)

    code = toss_live_smoke.main(
        [
            "--confirm",
            "--market",
            "kr",
            "--symbol",
            "005930",
            "--quantity",
            "1",
            "--price",
            "50000",
        ]
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED is not truthy" in output


@pytest.mark.asyncio
async def test_run_confirm_places_retries_cancels_and_reconciles(monkeypatch, capsys) -> None:
    place_calls: list[dict[str, object]] = []
    cancel_calls: list[str] = []
    reconcile_calls: list[dict[str, object]] = []

    async def fake_place_for_smoke(**kwargs):
        place_calls.append(kwargs)
        return {
            "success": True,
            "order_id": "ord-1",
            "client_order_id": kwargs["client_order_id_override"],
            "ledger_id": 123,
            "mutation_sent": True,
        }

    async def fake_cancel_order(*, order_id, dry_run, confirm, account_mode):
        cancel_calls.append(order_id)
        return {
            "success": True,
            "original_order_id": order_id,
            "replacement_order_id": f"cancel-{order_id}",
            "mutation_sent": True,
        }

    async def fake_reconcile(**kwargs):
        reconcile_calls.append(kwargs)
        return {
            "success": True,
            "dry_run": kwargs["dry_run"],
            "counts": {"cancelled": 1},
            "reconciled": [{"order_id": kwargs["order_id"], "verdict": "none"}],
        }

    monkeypatch.setattr(toss_live_smoke, "_place_order_for_smoke", fake_place_for_smoke)
    monkeypatch.setattr(toss_live_smoke, "toss_cancel_order", fake_cancel_order)
    monkeypatch.setattr(toss_live_smoke, "toss_reconcile_orders_impl", fake_reconcile)

    code = await toss_live_smoke.run_confirm(
        market="kr",
        symbol="005930",
        quantity="1",
        price="50000",
        time_in_force="DAY",
    )

    assert code == 0
    assert len(place_calls) == 2
    assert place_calls[0]["client_order_id_override"] == place_calls[1]["client_order_id_override"]
    assert cancel_calls == ["ord-1"]
    assert reconcile_calls == [
        {"order_id": "ord-1", "symbol": "005930", "market": "kr", "dry_run": True, "limit": 10},
        {"order_id": "ord-1", "symbol": "005930", "market": "kr", "dry_run": False, "limit": 10},
    ]
    output = capsys.readouterr().out
    assert "client_secret" not in output.lower()
    assert "toss_confirm_place" in output
    assert "toss_confirm_cancel" in output
    assert "toss_confirm_reconcile_apply" in output


@pytest.mark.asyncio
async def test_run_confirm_cancels_duplicate_order_if_idempotency_fails(monkeypatch) -> None:
    order_ids = iter(["ord-1", "ord-2"])
    cancel_calls: list[str] = []

    async def fake_place_for_smoke(**kwargs):
        return {
            "success": True,
            "order_id": next(order_ids),
            "client_order_id": kwargs["client_order_id_override"],
            "ledger_id": 123,
            "mutation_sent": True,
        }

    async def fake_cancel_order(*, order_id, dry_run, confirm, account_mode):
        cancel_calls.append(order_id)
        return {"success": True, "original_order_id": order_id}

    async def fake_reconcile(**kwargs):
        return {"success": True, "counts": {"cancelled": 1}, "reconciled": []}

    monkeypatch.setattr(toss_live_smoke, "_place_order_for_smoke", fake_place_for_smoke)
    monkeypatch.setattr(toss_live_smoke, "toss_cancel_order", fake_cancel_order)
    monkeypatch.setattr(toss_live_smoke, "toss_reconcile_orders_impl", fake_reconcile)

    code = await toss_live_smoke.run_confirm(
        market="kr",
        symbol="005930",
        quantity="1",
        price="50000",
        time_in_force="DAY",
    )

    assert code == 2
    assert cancel_calls == ["ord-1", "ord-2"]


@pytest.mark.asyncio
async def test_run_confirm_cancels_original_when_idempotency_retry_raises(monkeypatch) -> None:
    calls = 0
    cancel_calls: list[str] = []

    async def fake_place_for_smoke(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "success": True,
                "order_id": "ord-1",
                "client_order_id": kwargs["client_order_id_override"],
                "ledger_id": 123,
                "mutation_sent": True,
            }
        raise RuntimeError("retry exploded")

    async def fake_cancel_order(*, order_id, dry_run, confirm, account_mode):
        cancel_calls.append(order_id)
        return {"success": True, "original_order_id": order_id}

    async def fake_reconcile(**kwargs):
        return {"success": True, "counts": {"cancelled": 1}, "reconciled": []}

    monkeypatch.setattr(toss_live_smoke, "_place_order_for_smoke", fake_place_for_smoke)
    monkeypatch.setattr(toss_live_smoke, "toss_cancel_order", fake_cancel_order)
    monkeypatch.setattr(toss_live_smoke, "toss_reconcile_orders_impl", fake_reconcile)

    code = await toss_live_smoke.run_confirm(
        market="kr",
        symbol="005930",
        quantity="1",
        price="50000",
        time_in_force="DAY",
    )

    assert code == 2
    assert cancel_calls == ["ord-1"]


