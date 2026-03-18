"""Debug script: verify Finnhub economic calendar returns real data."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta

from app.core.config import settings


async def main() -> None:
    # 1. Check API key
    api_key = settings.finnhub_api_key
    if not api_key:
        print("ERROR: FINNHUB_API_KEY is not set")
        sys.exit(1)
    print(f"API Key: {api_key[:8]}...{api_key[-4:]}")

    # 2. Raw Finnhub call
    import finnhub

    client = finnhub.Client(api_key=api_key)

    # Test with a date range covering known events
    # FOMC 2026-03-19, or use a wider range to ensure hits
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    if target_date is None:
        # Default: today + next 3 days
        today = datetime.now()
        from_date = today.strftime("%Y-%m-%d")
        to_date = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    else:
        from_date = target_date
        to_date = target_date

    print(f"\nFetching economic calendar: {from_date} to {to_date}")

    raw_response = client.calendar_economic(_from=from_date, to=to_date)
    print(f"\nRaw response type: {type(raw_response).__name__}")

    if isinstance(raw_response, dict):
        events = raw_response.get("economicCalendar", [])
        print(f"Total events in response: {len(events)}")

        us_events = [e for e in events if str(e.get("country", "")).upper() == "US"]
        print(f"US events: {len(us_events)}")

        if us_events:
            print("\n--- US Events ---")
            for e in us_events[:20]:
                print(json.dumps(e, indent=2, default=str))
        else:
            print("\nNo US events found. Showing all countries:")
            countries = set(str(e.get("country", "?")) for e in events[:50])
            print(f"Countries in response: {countries}")
            if events:
                print(f"\nFirst event sample: {json.dumps(events[0], indent=2, default=str)}")
    else:
        print(f"Unexpected response: {raw_response}")

    # 3. Test through our service layer
    print("\n\n--- Service Layer Test ---")
    from app.services.external.economic_calendar import (
        _clear_economic_calendar_cache,
        fetch_economic_events_today,
    )

    _clear_economic_calendar_cache()
    result = await fetch_economic_events_today()
    print(f"fetch_economic_events_today returned {len(result)} events")
    for event in result:
        print(f"  {event['time']} | {event['event']} | {event['importance']}")


if __name__ == "__main__":
    asyncio.run(main())
