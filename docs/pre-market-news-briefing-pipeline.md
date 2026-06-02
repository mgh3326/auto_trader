# Pre-Market News Briefing Pipeline

> Operator-facing documentation for the OpenClaw-driven pre-market news workflow.

## Architecture Summary

```
n8n (wake-only trigger)
    ↓
OpenClaw Desktop (crawling + analysis)
    ↓
POST /api/v1/news/bulk  ←───┐
                              │
auto_trader (storage)  →  GET /api/v1/news
                              │
                         GET /api/n8n/news
                              │
                    MCP get_market_news
                              ↓
                    OpenClaw (briefing generation)
                              ↓
                    Discord output
```

**Key principle**: n8n is wake-only; OpenClaw owns Desktop crawling and briefing logic; auto_trader stores and serves crawled news.

## source vs feed_source Conventions

| Field | Meaning | Example |
|-------|---------|---------|
| `source` | Publisher/provider label shown in briefing | `연합뉴스`, `매일경제`, `유안타증권` |
| `feed_source` | Collection path key used for provenance/filtering | `browser_naver_mainnews`, `browser_naver_research`, `rss_mk`, `rss_yna` |

### Supported feed_source Naming Examples

- `browser_naver_mainnews` - Naver Finance main news (browser-crawled)
- `browser_naver_research` - Naver Finance research reports (browser-crawled)
- `rss_mk` - 매일경제 RSS
- `rss_yna` - 연합뉴스 RSS
- `rss_hankyung` - 한국경제 RSS

## Write Path: Ingest Crawled News

OpenClaw Desktop crawls and sends to:

```bash
POST /api/v1/news/bulk
Content-Type: application/json

{
  "articles": [
    {
      "url": "https://finance.naver.com/news/news_read.naver?article_id=1",
      "title": "삼성전자 실적 기대감 확대",
      "source": "연합뉴스",
      "feed_source": "browser_naver_mainnews",
      "published_at": "2026-03-30T08:10:00+09:00",
      "keywords": ["삼성전자", "반도체"],
      "stock_symbol": "005930",
      "stock_name": "삼성전자"
    }
  ]
}
```

**Response**:
```json
{
  "success": true,
  "inserted_count": 1,
  "skipped_count": 0,
  "skipped_urls": []
}
```

## Read Path: Query News

### GET /api/v1/news

Primary API for news retrieval with filtering:

```bash
curl "http://localhost:8000/api/v1/news?hours=3&feed_source=browser_naver_mainnews&keyword=삼성"
```

Query parameters:
- `hours` - Lookback period (1-720)
- `feed_source` - Filter by collection path key
- `source` - Filter by publisher label
- `keyword` - Filter by keyword in title/content
- `has_analysis` - Filter by analysis completion status
- `limit` - Maximum results (default 10, max 100)

### GET /api/n8n/news

Discord-formatted endpoint for n8n integration:

```bash
curl "http://localhost:8000/api/n8n/news?hours=3&feed_source=browser_naver_research&source=유안타증권"
```

Response includes:
- `items` - News articles with Discord-ready formatting
- `summary.sources` - Unique publisher names
- `summary.feed_sources` - Unique collection path keys
- `discord_title` - Pre-formatted title
- `discord_body` - Pre-formatted body with quotes and links

### MCP get_market_news (Legacy - Briefing Only)

> [!NOTE]
> `get_market_news` is a legacy broad-market briefing surface. It is kept solely to support the pre-market briefing pipeline and must NOT be used for investment-decision evidence. The dead `search_news` MCP tool has been entirely removed as part of ROB-424.

```python
# Example MCP call
get_market_news(
    hours=24,
    feed_source="browser_naver_mainnews",
    source="연합뉴스",
    keyword="반도체",
    limit=20
)
```

Response includes:
- `count` - Number of articles returned
- `total` - Total matching articles
- `news` - Article list with `stock_symbol` and `stock_name`
- `sources` - Unique publisher names (for briefing segmentation)
- `feed_sources` - Unique collection path keys (for provenance)

## Dedupe Policy

**URL-only dedupe** is the current production rule.

- Identical URLs are skipped during bulk insert
- No title-based or fuzzy dedupe is performed
- Re-inserting the same URL returns `skipped_count: 1` with URL in `skipped_urls`

## Why No content_type Migration

The `news_articles` table already has sufficient fields for the briefing pipeline:

- `source` - Publisher display
- `feed_source` - Collection path (encodes content type via naming convention)
- `keywords` - Keyword/holding mapping
- `stock_symbol`/`stock_name` - Stock linkage

Content type boundaries are encoded via `feed_source` naming conventions (e.g., `browser_naver_mainnews` vs `browser_naver_research`) rather than a separate database column.

## Manual Verification

### Insert sample articles

```bash
cat > /tmp/sample-news.json << 'EOF'
{
  "articles": [
    {
      "url": "https://example.com/news/1",
      "title": "삼성전자 실적 발표",
      "source": "연합뉴스",
      "feed_source": "browser_naver_mainnews",
      "published_at": "2026-03-30T08:10:00+09:00",
      "keywords": ["삼성전자", "반도체"],
      "stock_symbol": "005930",
      "stock_name": "삼성전자"
    },
    {
      "url": "https://example.com/news/2",
      "title": "반도체 시장 전망",
      "source": "매일경제",
      "feed_source": "browser_naver_mainnews",
      "published_at": "2026-03-30T08:30:00+09:00",
      "keywords": ["반도체", "시장전망"]
    }
  ]
}
EOF

curl -X POST http://localhost:8000/api/v1/news/bulk \
  -H "Content-Type: application/json" \
  -d @/tmp/sample-news.json
```

### Verify re-insertion skips duplicates

```bash
curl -X POST http://localhost:8000/api/v1/news/bulk \
  -H "Content-Type: application/json" \
  -d @/tmp/sample-news.json
# Expected: skipped_count: 2, skipped_urls contains both URLs
```

### Query via API endpoints

```bash
# Query all recent news
curl "http://localhost:8000/api/n8n/news?hours=3"

# Filter by feed_source
curl "http://localhost:8000/api/n8n/news?hours=3&feed_source=browser_naver_mainnews"

# Filter by publisher
curl "http://localhost:8000/api/n8n/news?hours=3&source=연합뉴스"

# Combined filters
curl "http://localhost:8000/api/n8n/news?hours=3&feed_source=browser_naver_mainnews&source=연합뉴스&keyword=삼성"
```

### Query via MCP

```bash
# Using mcp-cli or similar
mcp call auto_trader get_market_news '{"hours": 3, "feed_source": "browser_naver_mainnews"}'
```

## Rollback Notes

- No schema migration was performed - rollback is code-only
- URL-only dedupe remains the production rule
- `feed_source` naming conventions encode content type
- n8n remains wake-only; OpenClaw owns crawling, analysis, and Discord output
