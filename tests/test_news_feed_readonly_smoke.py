from __future__ import annotations

from scripts.news_feed_readonly_smoke import run_smoke, validate_feed_payload


def test_validate_feed_payload_requires_additive_fields():
    result = validate_feed_payload(
        "/invest/api/feed/news?tab=us&limit=20",
        {
            "items": [
                {
                    "id": 1,
                    "title": "S&P 500 rallies as big tech climbs",
                    "market": "us",
                    "url": "https://example.com/news",
                    "relatedSymbols": [],
                    "scope": "market_wide",
                    "tags": ["broad_market"],
                    "category": None,
                    "noiseReason": None,
                }
            ]
        },
    )

    assert result.ok is True
    assert result.item_count == 1
    assert result.warnings == []


def test_validate_feed_payload_reports_missing_fields():
    result = validate_feed_payload(
        "/invest/api/feed/news?tab=latest&limit=20",
        {"items": [{"id": 1, "title": "Missing ROB-155 fields"}]},
    )

    assert result.ok is False
    assert "item_0_missing_scope" in result.errors
    assert "item_0_missing_tags" in result.errors


def test_validate_feed_payload_warns_crypto_without_category_distribution():
    result = validate_feed_payload(
        "/invest/api/feed/news?tab=crypto&limit=20",
        {
            "items": [
                {
                    "id": 2,
                    "title": "Bitcoin moves",
                    "market": "crypto",
                    "url": "https://example.com/crypto",
                    "relatedSymbols": [],
                    "scope": "symbol_specific",
                    "tags": [],
                    "category": None,
                    "noiseReason": None,
                }
            ]
        },
    )

    assert result.ok is True
    assert "crypto_items_present_but_no_category_distribution" in result.warnings


def test_run_smoke_uses_get_only_fetcher(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def fake_fetch(base_url, path, timeout, auth_header):
        calls.append((path, auth_header))
        return {
            "items": [
                {
                    "id": 1,
                    "title": "ok",
                    "market": "us",
                    "url": "https://example.com/news",
                    "relatedSymbols": [],
                    "scope": "symbol_specific",
                    "tags": [],
                    "category": "market_price" if "crypto" in path else None,
                    "noiseReason": None,
                }
            ]
        }

    monkeypatch.setattr("scripts.news_feed_readonly_smoke._fetch_json", fake_fetch)

    results = run_smoke("https://example.com", timeout=1, auth_header="[REDACTED]")

    assert all(r.ok for r in results)
    assert [path for path, _ in calls] == [
        "/invest/api/feed/news?tab=latest&limit=20",
        "/invest/api/feed/news?tab=us&limit=20",
        "/invest/api/feed/news?tab=crypto&limit=20",
    ]
    assert all(auth == "[REDACTED]" for _, auth in calls)
