# Plan C: Economic Calendar API Research Plan

> **For Claude:** After research completes, create a follow-up implementation plan based on findings.

**Goal:** Research and select the best economic calendar API for fetching high-impact US economic events (FOMC, CPI, NFP, etc.)

**Architecture:** TBD based on research findings. Options include: direct API integration, scraping (not recommended), or using a financial data provider.

**Tech Stack:** TBD (depends on selected API)

---

## Research Tasks

### Task 1: Survey Economic Calendar APIs

**Research Questions:**
1. What free or low-cost economic calendar APIs are available?
2. What are their data coverage, accuracy, and update frequency?
3. What are the rate limits and pricing tiers?

**APIs to Research:**

#### Option 1: Trading Economics API
- **Website:** https://tradingeconomics.com/api/
- **Pros:** Comprehensive coverage, well-known provider
- **Cons:** Paid API ($30+/month), requires API key
- **Data:** US, EU, global economic events

#### Option 2: Forex Factory (Scraping - Not Recommended)
- **Website:** https://www.forexfactory.com/calendar
- **Pros:** Free, comprehensive
- **Cons:** No official API, TOS prohibits scraping, fragile
- **Verdict:** Skip - legal/technical issues

#### Option 3: MyFXBook Calendar API
- **Website:** https://www.myfxbook.com/forex-economic-calendar
- **Pros:** Free tier available, forex-focused
- **Cons:** Limited documentation
- **Research needed:** Check if API is public

#### Option 4: FRED (Federal Reserve Economic Data)
- **Website:** https://fred.stlouisfed.org/
- **Pros:** Official US government data, free API key
- **Cons:** Release dates only (not future calendar), not real-time events
- **Verdict:** Not suitable for "today's events" use case

#### Option 5: Economic Calendar Aggregator (DIY)
- **Approach:** Use multiple free sources + caching
- **Pros:** Free, customizable
- **Cons:** Maintenance burden, reliability issues

#### Option 6: Finnhub
- **Website:** https://finnhub.io/
- **Pros:** Free tier (60 calls/minute), existing integration in codebase
- **Cons:** Economic calendar coverage TBD
- **Research needed:** Check if Finnhub has economic calendar endpoint

#### Option 7: MarketWatch / Investing.com APIs
- **Research needed:** Check if they have public APIs

---

### Task 2: Evaluate Finnhub (Existing Integration)

**Context:** The codebase already uses Finnhub for US market data.

**Research Steps:**
1. Check Finnhub documentation for economic calendar endpoint
2. Test if `/calendar/economic` endpoint exists and works
3. Verify coverage of high-impact events (FOMC, CPI, NFP)

**Expected API Call:**
```bash
curl "https://finnhub.io/api/v1/calendar/economic?from=2026-03-16&to=2026-03-16&token=YOUR_API_KEY"
```

**Research Output:**
- Does the endpoint exist?
- What data is returned?
- Are high-impact events marked?
- Free tier limitations?

---

### Task 3: Evaluate Trading Economics (If Finnhub Insufficient)

**Research Steps:**
1. Check if there's a free tier or trial
2. Review API documentation for calendar endpoint
3. Test sample request for US events

**Expected API Call:**
```bash
curl "https://api.tradingeconomics.com/calendar?c=united+states&country=united+states& importance=3&client=YOUR_API_KEY"
```

**Research Output:**
- Pricing for our use case (low volume)
- Data quality and latency
- Event importance scoring

---

### Task 4: Define Event Importance Criteria

**High-Impact Events to Track:**
- FOMC meetings and interest rate decisions
- CPI (Consumer Price Index)
- PPI (Producer Price Index)
- NFP (Non-Farm Payrolls)
- GDP releases
- Unemployment rate
- Retail sales
- PMI data
- Treasury auctions (optional)

**Event Data Structure Needed:**
```python
{
    "time": "21:30 KST",           # Event time in KST
    "event": "US CPI",             # Event name
    "importance": "high",          # high/medium/low
    "previous": "2.4%",           # Previous value (optional)
    "forecast": "2.3%"            # Forecast value (optional)
}
```

---

### Task 5: Research Implementation Complexity

**For Each Viable Option, Assess:**

| Criteria | Weight | Option A | Option B | Option C |
|----------|--------|----------|----------|----------|
| Cost | High | ? | ? | ? |
| Data quality | High | ? | ? | ? |
| Implementation effort | Medium | ? | ? | ? |
| Maintenance burden | Medium | ? | ? | ? |
| Existing codebase fit | Medium | ? | ? | ? |

**Research Output:**
- Scored comparison matrix
- Recommended option with justification
- Fallback options

---

## Research Deliverables

### Deliverable 1: API Comparison Document

```markdown
## Economic Calendar API Comparison

### Option 1: Finnhub
- **Cost:** Free tier (60 calls/min)
- **Endpoint:** `/calendar/economic`
- **Coverage:** [TO BE RESEARCHED]
- **Implementation:** Easy (existing integration)
- **Verdict:** [TBD]

### Option 2: Trading Economics
- **Cost:** $30/month
- **Endpoint:** `/calendar`
- **Coverage:** Comprehensive
- **Implementation:** Medium (new integration)
- **Verdict:** [TBD]

### Option 3: [Other]
...

## Recommendation
**Selected:** [API Name]
**Rationale:** [Why this one]
**Fallback:** [If primary fails]
```

### Deliverable 2: Sample API Response

Actual JSON response from the selected API for testing.

### Deliverable 3: Implementation Sketch

Rough outline of how the service would be implemented.

---

## Post-Research Next Steps

### If Finnhub Works:
**Quick Implementation Plan (similar to Plan A):**
1. Add `fetch_economic_events()` to existing Finnhub client
2. Update `economic_calendar.py` to call Finnhub
3. Transform response to `N8nEconomicEvent` format
4. Add tests

**Estimated Time:** 2-3 hours

### If Trading Economics Required:
**Moderate Implementation Plan:**
1. Sign up for API key
2. Create new `trading_economics.py` service
3. Implement caching and error handling
4. Wire into market context service
5. Add tests and documentation

**Estimated Time:** 4-6 hours

### If No Suitable API Found:
**Alternative Approaches:**
1. **Defer implementation:** Keep stub, document limitation
2. **Manual curation:** Hardcode major event dates (FOMC schedule known in advance)
3. **Webhook integration:** Use external service to push events

---

## Execution

**Start Research Command:**

```bash
# Use librarian subagent to research APIs
# Research Finnhub first (existing integration)
curl "https://finnhub.io/docs/api/company-earnings" # Check if calendar endpoint exists

# Then research Trading Economics if needed
curl "https://tradingeconomics.com/api/"
```

**Research Session:**
- Use `task(subagent_type="librarian", ...)` for parallel research
- Query: "Finnhub economic calendar API endpoint documentation"
- Query: "Trading Economics API calendar endpoint free tier"

---

## Decision Gate

After research completes, ask:

> **Research findings ready. Recommended approach:**
> 
> **Option X: [API Name]**
> - Cost: $X/month or Free
> - Effort: Y hours
> - Data quality: [Good/Fair/Excellent]
> 
> **Create implementation plan?** (Y/n)
> 
> Or would you like to:
> - A) Proceed with Option X
> - B) Consider alternative approaches
> - C) Defer economic calendar implementation

---

**Plan saved to:** `docs/plans/YYYY-MM-DD-economic-calendar-research.md`

**Note:** This is a research plan only. No code changes until research completes and implementation plan is approved.
