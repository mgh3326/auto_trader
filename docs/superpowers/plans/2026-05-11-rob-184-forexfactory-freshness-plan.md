# ROB-184 — ForexFactory freshness: TDD implementation plan

> **Companion spec:** `docs/superpowers/specs/2026-05-11-rob-184-forexfactory-freshness-design.md`
> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:test-driven-development`
> for the helper changes and `superpowers:subagent-driven-development` or
> `superpowers:executing-plans` for the per-task loop. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Make ForexFactory `(source=forexfactory, category=economic,
market=global)` ingestion durable across the upstream's rolling 14-day window
with weekly-aware caching, typed 429 / transient-error handling, and explicit
operator-visible behavior for dates the upstream cannot serve.

**Branch:** `feature/ROB-184-forexfactory-freshness` (already created)
**PR base:** `main`
**Linear:** ROB-184
**Implementer model preference:** Claude Code Sonnet (per Kanban task
boundary). If runtime cannot enforce, record the limitation in the Linear
trail rather than claim enforcement.

---

## Safety preconditions (must be true before any step runs)

* [ ] Working directory is `/Users/mgh3326/worktrees/auto_trader/rob-184-forexfactory-freshness`
  (worktree for this issue). **Do not** edit the shared checkout at
  `/Users/mgh3326/services/auto_trader/current` or any other worktree.
* [ ] No broker / order / watch / order-intent / live or paper trading code is
  touched.
* [ ] No production DB write / backfill / Prefect schedule activation is
  performed by this PR. The runbook section adds the explicit approval-gate
  language but does not enable anything.
* [ ] CI must not call `nfs.faireconomy.media`. All tests patch the fetch seam.
* [ ] Linear trail records: branch, commit SHAs, exact test commands + output,
  and a "no live FF HTTP" statement.

---

## File map (all paths relative to repo root)

### Modified

* `app/services/market_events/forexfactory_helpers.py` — introduce
  `ForexFactoryWeeklyCache`, `ForexFactoryFetchError`,
  `rolling_window_for_today`, retry+jitter inside `_fetch_xml_documents`,
  thread `cache` parameter through `fetch_forexfactory_events_for_date`.
* `app/services/market_events/ingestion.py` —
  `ingest_economic_events_for_date` recognizes `None` rows (out-of-window)
  and `ForexFactoryFetchError.reason` to mark the partition `failed` with
  a sentinel `last_error` string.
* `tests/services/test_market_events_forexfactory_helpers.py` — new cases
  in §3.
* `tests/services/test_market_events_ingestion.py` — new cases in §3.
* `tests/test_market_events_cli.py` — dry-run no-fetch assertion.
* `docs/runbooks/market-events-ingestion.md` — append a "ROB-184 freshness +
  upstream rolling window" subsection inside the ROB-132 economic-events
  section, document new failure reasons, document the approval-gated Prefect
  deployment contract.
* `docs/runbooks/calendar-source-coverage.md` — add a row noting
  `out_of_rolling_window` as a structural-gap signal (not a bug).

### New

* `tests/test_frontend_no_forexfactory_request_path.py` — static-source guard.
* `app/flows/__init__.py` (only if not already present) and
  `app/flows/forexfactory_calendar_flow.py` — **unscheduled** Prefect flow
  stub, deployment not registered. Activation gated.

### Not touched

* `app/services/external/forexfactory_calendar.py` — legacy n8n / Telegram
  path. Keep as-is. Its tests
  (`tests/test_services_forexfactory_calendar.py`) must remain green.
* Any DB migration. No schema change.
* Frontend code. The static guard in
  `tests/test_frontend_no_forexfactory_request_path.py` only **reads**.

---

## Pre-flight

* [ ] **Step 0.1: Confirm worktree + clean tree**

  ```bash
  cd /Users/mgh3326/worktrees/auto_trader/rob-184-forexfactory-freshness
  git status
  git rev-parse --abbrev-ref HEAD
  ```

  Expected: branch `feature/ROB-184-forexfactory-freshness`, working tree
  clean (the spec + this plan are committed before any code change).

* [ ] **Step 0.2: Verify environment + baseline tests**

  ```bash
  uv sync --all-groups
  uv run pytest \
    tests/services/test_market_events_forexfactory_helpers.py \
    tests/services/test_market_events_ingestion.py \
    tests/test_market_events_cli.py \
    tests/test_services_forexfactory_calendar.py \
    -q
  ```

  Expected: all passing — this is the ROB-132 baseline plus ROB-178 guard.

* [ ] **Step 0.3: Record the test command + output in the Linear trail.**

---

## Task 1 (TDD) — Pure helpers: `rolling_window_for_today` + window check

**Why first:** pure function, no I/O, drives the "is this date servable by FF
at all" decision used by every later task.

### 1.1 Red — write failing test

* [ ] Add to `tests/services/test_market_events_forexfactory_helpers.py`:

  ```python
  from datetime import UTC, date, datetime
  from zoneinfo import ZoneInfo

  ET = ZoneInfo("America/New_York")


  @pytest.mark.unit
  def test_rolling_window_for_today_is_two_iso_weeks_in_et():
      from app.services.market_events.forexfactory_helpers import (
          rolling_window_for_today,
      )

      # Tue 2026-05-12 06:00 UTC == 02:00 ET (still Monday in ET? no — Tue)
      now_utc = datetime(2026, 5, 12, 6, 0, tzinfo=UTC)
      start, end = rolling_window_for_today(now_utc)
      # ISO-week containing 2026-05-12 ET starts Mon 2026-05-11; next week
      # ends Sun 2026-05-24. We anchor on Mon..Sun(+7) to match upstream feed.
      assert start == date(2026, 5, 11)
      assert end == date(2026, 5, 24)


  @pytest.mark.unit
  def test_rolling_window_for_today_handles_sunday_et_boundary():
      from app.services.market_events.forexfactory_helpers import (
          rolling_window_for_today,
      )

      # 2026-05-11 03:30 UTC == 2026-05-10 23:30 ET (Sunday)
      now_utc = datetime(2026, 5, 11, 3, 30, tzinfo=UTC)
      start, end = rolling_window_for_today(now_utc)
      # Sunday still belongs to "this week" feed (Mon 2026-05-04 .. Sun 2026-05-10)
      assert start == date(2026, 5, 4)
      assert end == date(2026, 5, 17)
  ```

  Run: should fail with `ImportError` / `AttributeError`.

### 1.2 Green — implement

* [ ] Add to `app/services/market_events/forexfactory_helpers.py` (top of
  module, after the existing imports):

  ```python
  from datetime import timedelta


  def rolling_window_for_today(now_utc: datetime) -> tuple[date, date]:
      """Return (start, end) inclusive of the rolling FF window in ET dates.

      Upstream publishes ISO-week thisweek + nextweek feeds anchored Monday
      in ET. The returned dates are ET-local calendar dates.
      """
      now_et = now_utc.astimezone(ET_TZ)
      today_et = now_et.date()
      # Python: Mon=0..Sun=6. Snap back to Monday.
      monday_this = today_et - timedelta(days=today_et.weekday())
      sunday_next = monday_this + timedelta(days=13)
      return monday_this, sunday_next
  ```

  Run the new tests. Expect green.

### 1.3 Refactor

* [ ] No additional refactor; `ET_TZ` already exists in this module.

### 1.4 Commit

* [ ] `git add app/services/market_events/forexfactory_helpers.py
  tests/services/test_market_events_forexfactory_helpers.py`
* [ ] `git commit -m "feat(ROB-184): add rolling_window_for_today helper"`

---

## Task 2 (TDD) — Typed fetch error + retry wrapper

### 2.1 Red

* [ ] Add to `tests/services/test_market_events_forexfactory_helpers.py`:

  ```python
  import httpx


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_fetch_xml_retries_429_with_retry_after_header(monkeypatch):
      from app.services.market_events import forexfactory_helpers as ff

      calls = {"n": 0}

      class _Resp:
          def __init__(self, status, text="<weeklyevents/>", retry_after=None):
              self.status_code = status
              self.text = text
              self.headers = {"Retry-After": retry_after} if retry_after else {}

          def raise_for_status(self):
              if self.status_code >= 400:
                  raise httpx.HTTPStatusError(
                      "boom", request=httpx.Request("GET", "x"), response=self
                  )

      async def fake_get(self, url, **kw):
          calls["n"] += 1
          if calls["n"] == 1:
              return _Resp(429, retry_after="0")
          return _Resp(200, text="<weeklyevents/>")

      monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
      # _fetch_one_xml is the new low-level helper exposed for the cache.
      text = await ff._fetch_one_xml(ff.THISWEEK_URL, max_attempts=3, base_delay=0)
      assert calls["n"] == 2
      assert text.startswith("<weeklyevents")


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_fetch_xml_raises_forexfactory_fetch_error_on_429_exhaustion(
      monkeypatch,
  ):
      from app.services.market_events import forexfactory_helpers as ff

      async def always_429(self, url, **kw):
          class _R:
              status_code = 429
              headers = {"Retry-After": "0"}
              text = ""

              def raise_for_status(self):
                  raise httpx.HTTPStatusError(
                      "x", request=httpx.Request("GET", url), response=self
                  )

          return _R()

      monkeypatch.setattr(httpx.AsyncClient, "get", always_429)
      with pytest.raises(ff.ForexFactoryFetchError) as exc_info:
          await ff._fetch_one_xml(ff.THISWEEK_URL, max_attempts=3, base_delay=0)
      assert exc_info.value.reason == "rate_limited"


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_fetch_xml_does_not_retry_on_403(monkeypatch):
      from app.services.market_events import forexfactory_helpers as ff

      async def fake_get(self, url, **kw):
          class _R:
              status_code = 403
              headers = {}
              text = ""

              def raise_for_status(self):
                  raise httpx.HTTPStatusError(
                      "x", request=httpx.Request("GET", url), response=self
                  )

          return _R()

      monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
      with pytest.raises(ff.ForexFactoryFetchError) as exc:
          await ff._fetch_one_xml(ff.THISWEEK_URL, max_attempts=3, base_delay=0)
      assert exc.value.reason == "upstream_4xx"
  ```

  Run: expect failure.

### 2.2 Green

* [ ] Add to `app/services/market_events/forexfactory_helpers.py`:

  ```python
  import asyncio
  import random

  RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


  class ForexFactoryFetchError(Exception):
      def __init__(self, reason: str, *, cause: Exception | None = None):
          super().__init__(f"forexfactory_fetch_error:{reason}")
          self.reason = reason
          self.__cause__ = cause


  async def _sleep_with_jitter(seconds: float) -> None:
      jittered = seconds * (1 + random.uniform(-0.25, 0.25))
      await asyncio.sleep(max(jittered, 0))


  async def _fetch_one_xml(
      url: str,
      *,
      max_attempts: int = 3,
      base_delay: float = 1.0,
  ) -> str:
      last_exc: Exception | None = None
      for attempt in range(max_attempts):
          try:
              async with httpx.AsyncClient(timeout=10.0) as client:
                  response = await client.get(url)
                  if response.status_code in RETRIABLE_STATUS:
                      retry_after_hdr = response.headers.get("Retry-After")
                      retry_after = float(retry_after_hdr) if retry_after_hdr else None
                      delay = retry_after if retry_after is not None else min(
                          base_delay * (2**attempt), 30.0
                      )
                      if attempt < max_attempts - 1:
                          await _sleep_with_jitter(delay)
                          continue
                      reason = (
                          "rate_limited" if response.status_code == 429 else "upstream_5xx"
                      )
                      raise ForexFactoryFetchError(reason)
                  response.raise_for_status()
                  return response.text
          except httpx.HTTPStatusError as exc:
              status = exc.response.status_code if exc.response is not None else 0
              if status in RETRIABLE_STATUS and attempt < max_attempts - 1:
                  last_exc = exc
                  await _sleep_with_jitter(min(base_delay * (2**attempt), 30.0))
                  continue
              if status == 429:
                  raise ForexFactoryFetchError("rate_limited", cause=exc) from exc
              if 500 <= status < 600:
                  raise ForexFactoryFetchError("upstream_5xx", cause=exc) from exc
              raise ForexFactoryFetchError("upstream_4xx", cause=exc) from exc
          except (httpx.TimeoutException, httpx.TransportError) as exc:
              last_exc = exc
              if attempt < max_attempts - 1:
                  await _sleep_with_jitter(min(base_delay * (2**attempt), 30.0))
                  continue
              raise ForexFactoryFetchError("network_error", cause=exc) from exc
      raise ForexFactoryFetchError("unknown", cause=last_exc)
  ```

  Run: expect green.

### 2.3 Refactor

* [ ] The legacy `_fetch_xml_documents` is rewritten in Task 3 to delegate to
  `_fetch_one_xml`. Defer that here.

### 2.4 Commit

* [ ] `git commit -m "feat(ROB-184): typed fetch + 429/5xx retry for forexfactory"`

---

## Task 3 (TDD) — `ForexFactoryWeeklyCache`

### 3.1 Red

* [ ] Add to `tests/services/test_market_events_forexfactory_helpers.py`:

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_weekly_cache_fetches_each_url_at_most_once(monkeypatch):
      from app.services.market_events import forexfactory_helpers as ff

      call_log: list[str] = []

      async def fake_fetch(url, **kw):
          call_log.append(url)
          if url == ff.THISWEEK_URL:
              return SAMPLE_XML  # defined at top of this test file
          return "<weeklyevents/>"

      monkeypatch.setattr(ff, "_fetch_one_xml", fake_fetch)

      cache = ff.ForexFactoryWeeklyCache(
          now_utc=datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
      )
      rows_a = await cache.get_events_for_date(date(2026, 5, 13))
      rows_b = await cache.get_events_for_date(date(2026, 5, 14))
      assert rows_a is not None
      assert rows_b is not None
      # Both dates are in the same "thisweek" payload, so we expect 1 fetch only.
      assert call_log.count(ff.THISWEEK_URL) == 1


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_weekly_cache_fetches_nextweek_only_when_needed(monkeypatch):
      from app.services.market_events import forexfactory_helpers as ff

      call_log: list[str] = []

      async def fake_fetch(url, **kw):
          call_log.append(url)
          return SAMPLE_XML if url == ff.THISWEEK_URL else "<weeklyevents/>"

      monkeypatch.setattr(ff, "_fetch_one_xml", fake_fetch)

      cache = ff.ForexFactoryWeeklyCache(
          now_utc=datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
      )
      # 2026-05-13 belongs to thisweek (Mon 2026-05-11..Sun 2026-05-17)
      await cache.get_events_for_date(date(2026, 5, 13))
      assert ff.NEXTWEEK_URL not in call_log
      # 2026-05-19 is in the nextweek window (Mon 2026-05-18..Sun 2026-05-24)
      await cache.get_events_for_date(date(2026, 5, 19))
      assert ff.NEXTWEEK_URL in call_log


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_weekly_cache_returns_none_for_dates_outside_window(monkeypatch):
      from app.services.market_events import forexfactory_helpers as ff

      async def fake_fetch(url, **kw):
          return SAMPLE_XML

      monkeypatch.setattr(ff, "_fetch_one_xml", fake_fetch)
      cache = ff.ForexFactoryWeeklyCache(
          now_utc=datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
      )
      assert await cache.get_events_for_date(date(2026, 4, 30)) is None
      assert await cache.get_events_for_date(date(2026, 5, 30)) is None
  ```

  Run: expect failure (`ForexFactoryWeeklyCache` undefined).

### 3.2 Green

* [ ] In `app/services/market_events/forexfactory_helpers.py`:

  ```python
  class ForexFactoryWeeklyCache:
      def __init__(self, *, now_utc: datetime | None = None) -> None:
          self._now_utc = now_utc or datetime.now(UTC)
          self._window_start, self._window_end = rolling_window_for_today(
              self._now_utc
          )
          # thisweek covers [window_start, window_start + 6 days].
          self._thisweek_end = self._window_start + timedelta(days=6)
          self._payloads: dict[str, list[dict[str, Any]]] = {}

      def _url_for(self, target_date: date) -> str | None:
          if target_date < self._window_start or target_date > self._window_end:
              return None
          if target_date <= self._thisweek_end:
              return THISWEEK_URL
          return NEXTWEEK_URL

      async def _ensure_payload(self, url: str) -> list[dict[str, Any]]:
          cached = self._payloads.get(url)
          if cached is not None:
              return cached
          xml_text = await _fetch_one_xml(url)
          parsed = _parse_one_xml(xml_text)
          self._payloads[url] = parsed
          return parsed

      async def get_events_for_date(
          self, target_date: date
      ) -> list[dict[str, Any]] | None:
          url = self._url_for(target_date)
          if url is None:
              return None
          rows = await self._ensure_payload(url)
          return [r for r in rows if r["event_date"] == target_date]
  ```

* [ ] Update `fetch_forexfactory_events_for_date` to delegate:

  ```python
  async def fetch_forexfactory_events_for_date(
      target_date: date,
      *,
      cache: ForexFactoryWeeklyCache | None = None,
  ) -> list[dict[str, Any]] | None:
      """Return rows whose ET-day == target_date.

      Returns None when target_date is outside the upstream rolling window.
      Raises ForexFactoryFetchError on retry exhaustion or transport error.
      """
      cache = cache or ForexFactoryWeeklyCache()
      return await cache.get_events_for_date(target_date)
  ```

  Note: the **signature is backwards-incompatible only in the None case**. The
  existing test that mocks `_fetch_xml_documents` to return a list of XML
  docs is preserved with a shim: keep `_fetch_xml_documents` as a thin alias
  that calls `_fetch_one_xml` per URL, so existing tests stay green. Verify
  this by running the ROB-132 test file after the edits.

  Run all the helper tests. Expect green.

### 3.3 Refactor

* [ ] Remove now-dead `_fetch_xml_documents` if no callers (search the repo).
  If the alias is still patched by existing tests, leave the shim in place
  and add a `# kept for test compatibility — delete with the next refactor`
  comment.

### 3.4 Commit

* [ ] `git commit -m "feat(ROB-184): weekly-aware ForexFactoryWeeklyCache"`

---

## Task 4 (TDD) — Ingestion orchestrator: out-of-window + typed-error reasons

### 4.1 Red

* [ ] Add to `tests/services/test_market_events_ingestion.py`:

  ```python
  @pytest.mark.asyncio
  @pytest.mark.integration  # DB-backed
  async def test_economic_ingestion_marks_failed_out_of_rolling_window(db_session):
      from app.services.market_events.ingestion import (
          ingest_economic_events_for_date,
      )

      async def returns_none(_target_date):
          return None

      result = await ingest_economic_events_for_date(
          db_session,
          date(2026, 4, 1),  # arbitrary past date
          fetch_rows=returns_none,
      )
      assert result.status == "failed"
      assert result.error == "forexfactory_out_of_rolling_window"


  @pytest.mark.asyncio
  @pytest.mark.integration
  async def test_economic_ingestion_marks_failed_rate_limited(db_session):
      from app.services.market_events.forexfactory_helpers import (
          ForexFactoryFetchError,
      )
      from app.services.market_events.ingestion import (
          ingest_economic_events_for_date,
      )

      async def raises_rate_limited(_target_date):
          raise ForexFactoryFetchError("rate_limited")

      result = await ingest_economic_events_for_date(
          db_session,
          date(2026, 5, 13),
          fetch_rows=raises_rate_limited,
      )
      assert result.status == "failed"
      assert result.error == "forexfactory_rate_limited"
  ```

  Run: expect failure (the orchestrator currently treats `None` rows as
  "iterate over None" → TypeError, and any error becomes generic
  `last_error=str(exc)`).

### 4.2 Green

* [ ] In `app/services/market_events/ingestion.py`,
  `ingest_economic_events_for_date`:

  ```python
  try:
      from app.services.market_events.normalizers import (
          normalize_forexfactory_event_row,
      )
      from app.services.market_events.forexfactory_helpers import (
          ForexFactoryFetchError,
      )

      rows = await fetch_rows(target_date)
      if rows is None:
          await repo.mark_partition_failed(
              partition, error="forexfactory_out_of_rolling_window"
          )
          return IngestionRunResult(
              source=source,
              category=category,
              market=market,
              partition_date=target_date,
              status="failed",
              event_count=0,
              error="forexfactory_out_of_rolling_window",
          )
      ...  # existing upsert loop unchanged
  except ForexFactoryFetchError as exc:
      logger.warning(
          "forexfactory fetch failed for %s: %s", target_date, exc.reason
      )
      return await _mark_failed_after_exception(
          db,
          source=source,
          category=category,
          market=market,
          partition_date=target_date,
          error=Exception(f"forexfactory_{exc.reason}"),
      )
  ```

  Adjust the existing generic `except Exception` to come **after** the typed
  `ForexFactoryFetchError` handler.

  Run the new tests. Expect green.

### 4.3 Refactor

* [ ] Sanity-run the whole ingestion test file to confirm no regressions.

### 4.4 Commit

* [ ] `git commit -m "feat(ROB-184): typed failure reasons in forexfactory ingestion"`

---

## Task 5 — CLI: thread one cache through the per-day loop

### 5.1 Red

* [ ] Add to `tests/test_market_events_cli.py`:

  ```python
  @pytest.mark.asyncio
  async def test_cli_forexfactory_run_reuses_single_cache_across_days(monkeypatch):
      """A 14-day range must trigger at most two FF XML fetches (thisweek + nextweek)."""
      from scripts import ingest_market_events as cli
      from app.services.market_events import forexfactory_helpers as ff

      call_log: list[str] = []

      async def fake_fetch_one(url, **kw):
          call_log.append(url)
          return SAMPLE_XML

      monkeypatch.setattr(ff, "_fetch_one_xml", fake_fetch_one)
      monkeypatch.setattr(
          ff,
          "rolling_window_for_today",
          lambda now: (date(2026, 5, 11), date(2026, 5, 24)),
      )

      # Use the real ingest_economic_events_for_date with a single shared cache.
      ...  # builder calls cli.run_ingest with from=2026-05-11 to=2026-05-24

      assert call_log.count(ff.THISWEEK_URL) == 1
      assert call_log.count(ff.NEXTWEEK_URL) == 1


  @pytest.mark.asyncio
  async def test_cli_dry_run_does_not_call_forexfactory_fetch(monkeypatch):
      from scripts import ingest_market_events as cli
      from app.services.market_events import forexfactory_helpers as ff

      called = {"n": 0}

      async def boom(url, **kw):
          called["n"] += 1
          raise AssertionError("dry-run must not fetch")

      monkeypatch.setattr(ff, "_fetch_one_xml", boom)
      rc = await cli.main(
          [
              "--source", "forexfactory",
              "--category", "economic",
              "--market", "global",
              "--from-date", "2026-05-11",
              "--to-date", "2026-05-12",
              "--dry-run",
          ]
      )
      assert rc == 0
      assert called["n"] == 0
  ```

  Run: expect first test to fail (CLI currently builds a fresh cache per
  partition); second test should already pass but pin behavior.

### 5.2 Green

* [ ] In `scripts/ingest_market_events.py::run_ingest`, build the cache once
  for `forexfactory` runs:

  ```python
  ff_cache = None
  if (source, category, market) == ("forexfactory", "economic", "global"):
      from app.services.market_events.forexfactory_helpers import (
          ForexFactoryWeeklyCache,
      )
      ff_cache = ForexFactoryWeeklyCache()

  for d in iter_partition_dates(from_date, to_date):
      if dry_run:
          ...
      if ff_cache is not None:
          async def fetch_with_cache(target_date, _cache=ff_cache):
              return await _cache.get_events_for_date(target_date)

          result = await fn(db, d, fetch_rows=fetch_with_cache)
      else:
          result = await fn(db, d)
  ```

  Run the new CLI tests. Expect green.

### 5.3 Commit

* [ ] `git commit -m "feat(ROB-184): share weekly cache across per-day partitions in CLI"`

---

## Task 6 — Frontend static-guard test

### 6.1 Red

* [ ] Create `tests/test_frontend_no_forexfactory_request_path.py`:

  ```python
  """Static-source guard: SPA must not call ForexFactory directly (ROB-184)."""

  from pathlib import Path

  FRONTEND_SRC = Path(__file__).resolve().parents[1] / "frontend" / "invest" / "src"


  def _all_source_files() -> list[Path]:
      exts = {".ts", ".tsx", ".js", ".jsx"}
      return [p for p in FRONTEND_SRC.rglob("*") if p.suffix in exts]


  def test_frontend_does_not_reference_forexfactory_host():
      for path in _all_source_files():
          text = path.read_text(encoding="utf-8", errors="ignore")
          assert "nfs.faireconomy.media" not in text, path
          assert "ff_calendar_thisweek" not in text, path
          assert "ff_calendar_nextweek" not in text, path
  ```

  Run: expect green today (no frontend reference exists). The point is to
  catch a future regression.

### 6.2 Commit

* [ ] `git commit -m "test(ROB-184): guard SPA from request-path ForexFactory fetch"`

---

## Task 7 — Prefect flow stub (unscheduled, manually triggerable)

> This task only **writes the flow code** and **does not** register a Prefect
> deployment or schedule. Activation is a separate approval-gated task.

* [ ] Create `app/flows/forexfactory_calendar_flow.py` (or place under an
  existing flows directory if one exists; verify with `git ls-files
  app/flows`). Skeleton:

  ```python
  """ForexFactory economic-calendar ingestion flow (ROB-184).

  Activation is gated on 광현님 approval. Until then the flow is
  importable + manually invokable but no Prefect deployment is registered.
  """

  from __future__ import annotations

  from datetime import UTC, date, datetime, timedelta

  from prefect import flow, task

  from app.core.db import AsyncSessionLocal
  from app.services.market_events.forexfactory_helpers import (
      ForexFactoryWeeklyCache,
      rolling_window_for_today,
  )
  from app.services.market_events.ingestion import (
      ingest_economic_events_for_date,
  )


  @task
  async def run_one_day(target_date: date, cache: ForexFactoryWeeklyCache) -> dict:
      async def _fetch(d):
          return await cache.get_events_for_date(d)

      async with AsyncSessionLocal() as db:
          result = await ingest_economic_events_for_date(
              db, target_date, fetch_rows=_fetch
          )
          await db.commit()
          return result.model_dump()


  @flow(name="forexfactory_calendar_rolling_window")
  async def forexfactory_calendar_rolling_window_flow() -> list[dict]:
      now = datetime.now(UTC)
      start, end = rolling_window_for_today(now)
      cache = ForexFactoryWeeklyCache(now_utc=now)
      out = []
      cur = start
      while cur <= end:
          out.append(await run_one_day(cur, cache))
          cur += timedelta(days=1)
      return out
  ```

* [ ] Add a guard test
  `tests/test_forexfactory_calendar_flow_unscheduled.py` that asserts the flow
  imports cleanly but has no `prefect.deployments.Deployment` registration in
  the repo (grep-style guard).

* [ ] Commit:
  `git commit -m "feat(ROB-184): unscheduled prefect flow stub for forexfactory rolling window"`

---

## Task 8 — Runbook updates

* [ ] Append to `docs/runbooks/market-events-ingestion.md`, inside the
  existing "Economic events (ForexFactory, ROB-132)" section, a new
  subsection:

  ```markdown
  ### Freshness + rolling window (ROB-184)

  ForexFactory publishes only `thisweek.xml` and `nextweek.xml`. The rolling
  window any given run can serve is `[Mon of this ISO week, Sun of next ISO
  week]` in ET. Dates outside that window cannot be ingested from FF.

  **Partition `last_error` reasons introduced by ROB-184:**

  | `last_error` | Meaning | Operator action |
  | --- | --- | --- |
  | `forexfactory_out_of_rolling_window` | Date is older than the current rolling-week start or further out than next-week end. | None: this is a structural upstream limit. |
  | `forexfactory_rate_limited` | `nfs.faireconomy.media` returned 429 after retries. | Wait for the next scheduled run; consider lowering CLI parallelism. |
  | `forexfactory_upstream_5xx` | Upstream 5xx after retries. | Re-run the partition manually after upstream recovers. |
  | `forexfactory_network_error` | Transport / timeout error after retries. | Re-run; check ingest host network. |

  **Cache:** in-memory per CLI run / per Prefect flow run. The CLI builds a
  single `ForexFactoryWeeklyCache` and threads it through the per-day loop,
  so a 14-day range fetches `thisweek.xml` and `nextweek.xml` exactly once
  each.

  **Approval gate for production activation:** the Prefect flow stub at
  `app/flows/forexfactory_calendar_flow.py` is **not** scheduled. Activation
  requires explicit 광현님 approval recorded in Linear/Discord:

  1. `--dry-run` smoke from a deployed runner.
  2. Approval token + 광현님 confirmation for non-dry-run.
  3. Initial cadence: 4-hour Prefect schedule over `today..today+14`.

  No production DB backfill ships with this PR. Any one-time recovery
  (e.g., reingest a missing day inside the rolling window) follows the same
  approval gate.
  ```

* [ ] Append a single bullet to
  `docs/runbooks/calendar-source-coverage.md` under "Causes-of-empty-day
  taxonomy":

  ```markdown
  * `error (forexfactory_out_of_rolling_window)` — partition `last_error`
    matches this sentinel. UI follow-up task can remap to a softer
    "데이터 제공 범위 밖" label.
  ```

* [ ] Commit:
  `git commit -m "docs(ROB-184): document freshness fix + approval gate"`

---

## Task 9 — Full validation

* [ ] Run the focused suite:

  ```bash
  uv run pytest \
    tests/services/test_market_events_forexfactory_helpers.py \
    tests/services/test_market_events_ingestion.py \
    tests/test_market_events_cli.py \
    tests/test_services_forexfactory_calendar.py \
    tests/test_frontend_no_forexfactory_request_path.py \
    -q
  ```

  Expected: all green.

* [ ] Run the wider market-events suite to catch any regression:

  ```bash
  uv run pytest tests/services/test_market_events_ tests/test_market_events_ -q
  ```

  Expected: all green.

* [ ] Lint + format:

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  ```

* [ ] Capture all command outputs in the Linear trail under ROB-184.
  Explicitly note: **no live `nfs.faireconomy.media` HTTP fetch occurred
  during the test run** (the suite only patches `httpx.AsyncClient.get` /
  `_fetch_one_xml`).

---

## Task 10 — Handoff

* [ ] Push the branch:

  ```bash
  git push -u origin feature/ROB-184-forexfactory-freshness
  ```

* [ ] Open the PR (`base: main`):

  ```bash
  gh pr create --base main --title "feat(ROB-184): durable forexfactory freshness" --body "$(cat <<'EOF'
  ## Summary
  - Adds `ForexFactoryWeeklyCache` so a single CLI / Prefect run fetches
    `thisweek.xml` + `nextweek.xml` at most once each, even for a 14-day
    backfill.
  - Adds typed `ForexFactoryFetchError` with `Retry-After`-honoring
    backoff for 429 / 5xx / network errors. Max 3 attempts per URL per run.
  - Adds out-of-rolling-window detection: dates the upstream cannot serve
    are recorded as `partition.status="failed"` with sentinel
    `last_error="forexfactory_out_of_rolling_window"` so operators don't
    misread them as transient gaps.
  - Adds frontend static-source guard test to keep ForexFactory off the
    SPA request path.
  - Adds **unscheduled** Prefect flow stub. Activation requires 광현님
    approval (runbook updated).

  ## Test plan
  - [ ] `uv run pytest tests/services/test_market_events_forexfactory_helpers.py tests/services/test_market_events_ingestion.py tests/test_market_events_cli.py tests/test_services_forexfactory_calendar.py tests/test_frontend_no_forexfactory_request_path.py -q`
  - [ ] `uv run pytest tests/services/test_market_events_ tests/test_market_events_ -q`
  - [ ] `uv run ruff check . && uv run ruff format --check .`
  - [ ] Verify no live `nfs.faireconomy.media` fetch in CI logs.

  ## Safety
  - No broker / order / watch / live or paper trading code touched.
  - No DB migration. No new env vars.
  - No production schedule or backfill activated — approval-gated.

  Linear: ROB-184

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

* [ ] Post the PR URL + the full test output snippets to the Linear ROB-184
  issue thread. Tag 광현님 for review.

---

## Final checklist before marking the Kanban task `done`

* [ ] All tasks 1–9 committed and pushed.
* [ ] PR opened against `main`.
* [ ] Linear ROB-184 comment trail contains: branch, PR URL, commit SHAs, test
  commands + outputs, "no live FF HTTP" statement, link to this plan + the
  spec, and an explicit "no production schedule activation, no production
  backfill" note.
* [ ] Implementer model preference (Sonnet) recorded; if the runtime selected
  another model, the limitation is noted instead of claimed otherwise.
