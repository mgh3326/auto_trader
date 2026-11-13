from __future__ import annotations

from scripts import print_toss_token_namespace_fp


def test_cli_prints_only_the_configured_client_fingerprint(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_CLIENT_ID", "configured-client-id")

    print_toss_token_namespace_fp.main()

    assert capsys.readouterr().out == "b2a5c2ec9d0b6346\n"
