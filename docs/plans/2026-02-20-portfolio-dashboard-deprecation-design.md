# Unified Portfolio Dashboard + Legacy Deprecation Design

## 1) Goal

- Add a new read-only unified dashboard at `GET /portfolio/`.
- Add a unified API at `GET /portfolio/api/overview`.
- Deprecate legacy prefixes by returning `410 Gone` for all routes under:
  - `/manual-holdings/*`
  - `/kis-domestic-trading/*`
  - `/kis-overseas-trading/*`
  - `/upbit-trading/*`

## 2) Routing Design

### New Routes

- `GET /portfolio/`
  - Returns HTML dashboard page.
  - Screener-style shell with 2-column layout.
  - Initial one-time load + manual refresh only.

- `GET /portfolio/api/overview`
  - Query:
    - `market`: `ALL | KR | US | CRYPTO` (default: `ALL`)
    - `account_keys`: repeated query params (optional)
    - `q`: symbol/name search (optional)
  - Auth: existing `get_authenticated_user` dependency.

### Deprecated Legacy Routes

- Register centralized catch-all deprecated router for all legacy prefixes.
- Return behavior:
  - API request: `410` JSON response with:
    - `detail`
    - `replacement_url` (`/portfolio/`)
    - `deprecated_at`
  - HTML request: `410` HTML 안내 페이지.

## 3) Data Contract

`GET /portfolio/api/overview` response:

```json
{
  "success": true,
  "as_of": "ISO datetime",
  "filters": {
    "market": "ALL|KR|US|CRYPTO",
    "account_keys": ["..."],
    "q": "..."
  },
  "summary": {
    "total_positions": 0,
    "by_market": {
      "KR": 0,
      "US": 0,
      "CRYPTO": 0
    }
  },
  "facets": {
    "accounts": [
      {
        "account_key": "...",
        "broker": "...",
        "account_name": "...",
        "source": "live|manual",
        "market_types": ["KR", "US", "CRYPTO"]
      }
    ]
  },
  "positions": [
    {
      "market_type": "KR|US|CRYPTO",
      "symbol": "...",
      "name": "...",
      "quantity": 0,
      "avg_price": 0,
      "current_price": 0,
      "evaluation": 0,
      "profit_loss": 0,
      "profit_rate": 0,
      "components": []
    }
  ],
  "warnings": []
}
```

## 4) Aggregation Rules

- `coin` market alias is normalized to `CRYPTO`.
- Data sources:
  - KR/US: `KIS live holdings + manual holdings`
  - CRYPTO: `Upbit live holdings + manual holdings(CRYPTO)`
- Account key rules:
  - Live accounts: `live:kis`, `live:upbit`
  - Manual accounts: `manual:<broker_account_id>`
- Account toggle behavior:
  - If `account_keys` provided, aggregate using selected components only.
  - If omitted, aggregate all accounts.
- No retirement-specific parameter.
- KIS assumption in this phase: one connected real account.

## 5) UI Design

- Portfolio dashboard follows screener visual language:
  - Header + panel layout
  - 2-column desktop grid
  - Mobile card fallback
- Left area:
  - Market selector
  - Search input
  - Multi-account toggle list (broker + account name)
  - Result table / mobile cards
- Right area:
  - Position summary by market
  - Warnings/status panel
- Excluded column: `AI 결정/신뢰도`.

## 6) App Wiring Changes

- Keep `portfolio.router` and extend it with dashboard + overview endpoint.
- Remove legacy router includes from `app/main.py`:
  - `upbit_trading.router`
  - `kis_domestic_trading.router`
  - `kis_overseas_trading.router`
  - `manual_holdings.router`
- Add deprecated catch-all router include.

## 7) Navigation Changes

- Remove deprecated legacy menu links from `nav.html`.
- Keep/show:
  - `포트폴리오 (/portfolio/)`
  - `스크리너 (/screener)`

## 8) Test Scope

- Route/rendering:
  - `GET /portfolio/` returns 200 HTML and key DOM ids.
  - CSS mobile fallback rule exists.
- API:
  - Default `GET /portfolio/api/overview`.
  - `market` validation and filtering.
  - Repeated `account_keys` forwarding.
  - `q` filtering behavior.
  - Partial-source failure warning propagation.
- Deprecated:
  - Legacy page paths return `410` HTML.
  - Legacy API paths return `410` JSON schema.
- Navigation:
  - Deprecated links removed.
  - `/portfolio/` and `/screener` links present.

## 9) Rollback Points

- Re-enable legacy router includes in `app/main.py`.
- Remove deprecated catch-all include.
- Revert `nav.html` link updates.
- Keep `portfolio/api/overview` implementation isolated to new service and route additions.
