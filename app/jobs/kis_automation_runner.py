from app.jobs.kis_market_adapters import AutomationResult, SupportsMarketAutomation


async def run_market_automation(
    *,
    adapter: SupportsMarketAutomation,
) -> AutomationResult:
    if not adapter.market:
        raise ValueError("market is required")
    return await adapter.execute()
