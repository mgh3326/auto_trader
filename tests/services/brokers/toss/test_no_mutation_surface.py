from __future__ import annotations

from pathlib import Path

TOSS_DIR = Path("app/services/brokers/toss")


def test_toss_client_mutation_methods_use_account_required() -> None:
    source = (TOSS_DIR / "client.py").read_text()

    # Mutation methods must exist
    assert "async def place_order" in source
    assert "async def modify_order" in source
    assert "async def cancel_order" in source

    # And they should specify account_required=True
    # We will search the function definitions and check if account_required=True is nearby.
    place_def = source.split("async def place_order")[1].split("async def")[0]
    assert "account_required=True" in place_def

    modify_def = source.split("async def modify_order")[1].split("async def")[0]
    assert "account_required=True" in modify_def

    cancel_def = source.split("async def cancel_order")[1].split("async def")[0]
    assert "account_required=True" in cancel_def
