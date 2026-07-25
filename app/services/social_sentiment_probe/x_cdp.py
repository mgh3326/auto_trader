from __future__ import annotations

import datetime as dt
import json
from typing import Any
from urllib.parse import quote

from app.services.action_report.remote_debug_audit.cdp_client import CdpSession
from app.services.social_sentiment_probe.models import source_result, truncate_preview


def x_search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"


X_SEARCH_EXTRACT_JS = """
(function(){
  const rows = Array.from(document.querySelectorAll('article')).slice(0, 10);
  return JSON.stringify(rows.map((article) => {
    const links = Array.from(article.querySelectorAll('a')).map((a) => a.href);
    const statusUrl = links.find((href) => /\\/status\\//.test(href)) || null;
    const time = article.querySelector('time');
    const textNode = article.querySelector('[data-testid="tweetText"]');
    const userLink = links.find((href) => /^https:\\/\\/x\\.com\\/[^/]+$/.test(href));
    return {
      url: statusUrl,
      author: userLink ? userLink.replace('https://x.com/', '') : null,
      published_at: time ? time.getAttribute('datetime') : null,
      text: textNode ? textNode.innerText : null
    };
  }));
})()
"""

X_SEARCH_READY_JS = """
(function(){
  return document.querySelectorAll('article').length > 0 ||
    document.body.innerText.includes('Log in') ||
    document.body.innerText.includes('Something went wrong');
})()
"""


def parse_x_search_items(raw: Any) -> list[dict[str, Any]]:
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    items: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "title": None,
                "url": row.get("url"),
                "author": row.get("author"),
                "published_at": row.get("published_at"),
                "text_preview": truncate_preview(row.get("text")),
                "metrics": {},
            }
        )
    return items


async def fetch_x_search_cdp(
    query: str,
    market: str,
    cdp_session: CdpSession,
    *,
    limit: int = 10,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    try:
        raw = await cdp_session.fetch_rendered(
            x_search_url(query),
            X_SEARCH_EXTRACT_JS.replace(
                "slice(0, 10)", f"slice(0, {max(1, min(limit, 20))})"
            ),
            timeout_s=15.0,
            ready_js=X_SEARCH_READY_JS,
        )
    except Exception as exc:
        return source_result(
            source="x_cdp",
            market=market,
            query=query,
            status="unavailable",
            items=[],
            observed_at=observed_at,
            error_reason=f"{type(exc).__name__}: {exc}",
        )
    return source_result(
        source="x_cdp",
        market=market,
        query=query,
        status="ok",
        items=parse_x_search_items(raw),
        observed_at=observed_at,
    )
