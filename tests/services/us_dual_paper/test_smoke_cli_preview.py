import pytest

from scripts.smoke import us_dual_paper_preview_smoke as smoke


@pytest.mark.unit
def test_preview_mode_emits_packet(monkeypatch, capsys):
    monkeypatch.setenv("US_DUAL_PAPER_PREVIEW_ENABLED", "true")

    async def _fake_build(**kwargs):
        from app.schemas.us_dual_paper import (
            BrokerPreviewResult,
            DualBrokerPreviewPacket,
            DualPaperBrokerStatus,
        )

        return DualBrokerPreviewPacket(
            symbol=kwargs["symbol"],
            limit_price_source=kwargs["limit_price_source"],
            notional_cap_usd=kwargs["notional_cap_usd"],
            brokers={
                "alpaca_paper": BrokerPreviewResult(
                    account_scope="alpaca_paper", status=DualPaperBrokerStatus.PREVIEWED
                ),
            },
        )

    monkeypatch.setattr(smoke, "build_packet", _fake_build)
    rc = smoke.main(["--mode", "preview", "--symbol", "NVDA", "--quantity", "1",
                     "--limit-price", "10.0", "--notional-cap", "50"])
    assert rc == 0
    assert '"submit_enabled": false' in capsys.readouterr().out.lower()
