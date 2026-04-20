---
name: auto-trader-design
description: Use this skill to generate well-branded interfaces and assets for Auto Trader (multi-market AI-assisted trading workspace for KR / US / crypto), either for production work or throwaway prototypes, mocks, and screenshots. Contains essential design guidelines, color + type tokens, component specs, page patterns, and a migration plan from older Bootstrap-y pages to the newer warm-neutral fintech style.
user-invocable: true
---

Read the `README.md` file within this skill, and explore the other available files (`colors_and_type.css`, `preview/*.html`).

- For visual artifacts (slides, mocks, throwaway prototypes): copy `colors_and_type.css` into the artifact and build static HTML that follows the patterns documented in the README — shell + dark header band + warm-neutral panels.
- For production code changes in the `mgh3326/auto_trader` repo: apply the README's **Migration plan** section page-by-page. Prefer replacing per-page `:root` blocks with a shared import of `colors_and_type.css`.

Key touchstones:
- Target-direction templates: `screener_dashboard.html`, `portfolio_dashboard.html`, `pending_orders_dashboard.html`.
- Legacy pages to migrate: `login.html`, `register.html`, `admin_users.html`, `nav.html`, `base.html`, `error.html`.
- KR gain/loss convention: 상승 = red (`#9A1C1C`), 하락 = blue (`#0F4C9C`).
- Icons: Bootstrap Icons 1.11.3 via CDN. No emoji.
- Copy is operator-first, bilingual (Korean for chrome, English for technical/API terms).

If the user invokes this skill without guidance, ask what they want to build (new dashboard? migrate a legacy page? an isolated component?), ask a few follow-up questions about market + surface + density needs, and act as an expert designer who outputs HTML artifacts or production CSS, depending on the need.
