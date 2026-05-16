# `/invest/app/*` retirement inventory

> Follow-up deletion status (2026-05-16): the one-release-cycle soak
> window has completed. The retired legacy `/invest/app/*` pages,
> components, and component tests listed below were removed; canonical
> `/invest/*` routes and legacy redirects remain in `routes.tsx`.
> `AssetCategoryKey` now lives in `frontend/invest/src/types/filters.ts`.

Companion to `docs/plans/2026-05-08-invest-design-system-migration-implementation-plan.md`.

This Stage 6 doc inventories the legacy `/invest/app/*` mobile-only
routes and components, defines the redirect rules being shipped now,
and lists the files queued for deletion in a follow-up PR after a
soak window.

## Legacy → canonical redirects (this PR)

The Stage 6 PR ships only the redirect rules. Legacy components stay
mounted on the legacy import paths so the redirect hop is purely a URL
shape — no behaviour change for users who land on a legacy URL via a
bookmark or external link.

| Legacy URL                                         | Redirect target                          |
| -------------------------------------------------- | ---------------------------------------- |
| `/invest/app`                                      | `/invest/`                               |
| `/invest/app/discover`                             | `/invest/discover`                       |
| `/invest/app/discover/issues/:issueId`             | `/invest/discover/issues/:issueId`       |
| `/invest/app/paper`                                | `/invest/`                               |
| `/invest/app/paper/:variant`                       | `/invest/`                               |

Notes:
- The wildcard `*` route (kept) still redirects unknown paths to `/`.
- React-Router's `<Navigate replace>` is used for every redirect so
  history doesn't get polluted with the legacy URL.
- For the dynamic `/app/discover/issues/:issueId` redirect we render a
  `<DiscoverIssueRedirect>` shim that reads the param and emits the
  canonical path; static redirects use plain `<Navigate to=…>`.

## Legacy components — removed after soak

These files are no longer reachable from the live router after the
redirects ship, but stay in-tree for one release cycle so any
downstream tooling or external bookmarks have time to migrate. The
deletion happens in a separate later PR.

```
frontend/invest/src/pages/HomePage.tsx
frontend/invest/src/pages/DiscoverPage.tsx
frontend/invest/src/pages/DiscoverIssueDetailPage.tsx     # also reachable from canonical /discover/issues/:issueId — keep
frontend/invest/src/pages/PaperPlaceholderPage.tsx

frontend/invest/src/components/AppShell.tsx
frontend/invest/src/components/HeroCard.tsx               # superseded by components/home/DesktopHero.tsx + MobileHomePage inline hero
frontend/invest/src/components/AccountCardList.tsx
frontend/invest/src/components/AccountSelector.tsx
frontend/invest/src/components/AssetCategoryFilter.tsx    # AssetCategoryKey is exported from here; keep until consumers move to a shared types module
frontend/invest/src/components/HoldingRow.tsx             # superseded by components/home/HoldingsTable.tsx
frontend/invest/src/components/BottomNav.tsx              # superseded by mobile/MobileBottomNav.tsx

frontend/invest/src/components/discover/AiIssueCard.tsx
frontend/invest/src/components/discover/AiIssueTicker.tsx
frontend/invest/src/components/discover/CategoryShortcutRail.tsx
frontend/invest/src/components/discover/DiscoverCalendarCard.tsx
frontend/invest/src/components/discover/DiscoverHeader.tsx
frontend/invest/src/components/discover/IssueImpactMap.tsx
frontend/invest/src/components/discover/RelatedSymbolsList.tsx
```

`DiscoverIssueDetailPage` is **kept** because the canonical
`/discover/issues/:issueId` route reuses the same component.

`AssetCategoryFilter` is **kept** until Stage 6 follow-up because
`AssetCategoryKey` (its exported type) is consumed by
`components/home/FilterChips`, `pages/desktop/DesktopHomePage`,
`pages/mobile/MobileHomePage`, and `desktop/LeftContextRail`. The
follow-up will move that type into a shared `types/filters.ts`.

`severity.ts` (under `components/discover/`) is **kept** —
`pages/desktop/DesktopDiscoverPage` and `pages/mobile/MobileDiscoverPage`
both consume `describeDirection` and `sortMarketIssues`.

## Legacy tests — removed after soak

```
frontend/invest/src/__tests__/HomePage.test.tsx           # tests the legacy /app HomePage
frontend/invest/src/__tests__/AccountCardList.test.tsx
frontend/invest/src/__tests__/HoldingRow.test.tsx
frontend/invest/src/__tests__/BottomNav.test.tsx
frontend/invest/src/__tests__/AiIssueCard.test.tsx
frontend/invest/src/__tests__/AiIssueTicker.test.tsx       # if present
frontend/invest/src/__tests__/DiscoverPage.test.tsx
frontend/invest/src/__tests__/DiscoverCalendarCard.test.tsx
frontend/invest/src/__tests__/IssueImpactMap.test.tsx
frontend/invest/src/__tests__/RelatedSymbolsList.test.tsx
```

These will be deleted with their corresponding components, or
rewritten to target the canonical equivalents in the same follow-up.

## Open questions

- **`/app/paper`** — currently redirects to `/`. The placeholder
  was an internal stub; if the team wants to expose a dedicated
  `/invest/paper` surface (paper trading), open a separate ticket
  and add the canonical route there.
- **Soak window length** — recommend at least one full release cycle
  before the deletion PR.

## Deletion PR — separate, deferred

Track as a follow-up issue. Steps for the cleanup PR:

1. Delete the components + pages listed above.
2. Move `AssetCategoryKey` from `components/AssetCategoryFilter.tsx`
   to `types/filters.ts`; update consumers.
3. Delete or rewrite the legacy tests.
4. Run `npm run typecheck && npm test -- --run && npm run build`.
5. Verify the canonical routes still work end-to-end.
