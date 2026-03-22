# Hybrid Chart Screenshot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a hybrid technical-analysis image mode that embeds a real chart screenshot into the existing SVG layout while preserving the current SVG-only workflow as the default.

**Architecture:** Keep browser automation isolated in `ScreenshotCapture`, keep PNG-in-SVG composition isolated in `ImageComposer`, and make `StockAnalysisPreset` responsible only for selecting hybrid vs SVG-only rendering based on `screenshot_path`. Do not modify existing chart/dashboard/support-resistance components; hybrid mode should swap only the left chart region. Because embedded base64 PNGs can make SVGs large, the PNG conversion path must optionally delete only the temporary hybrid SVG after successful PNG generation.

**Tech Stack:** Python 3.13+, `uv`, pytest, Playwright (`blog/tools/svg_converter.py`), external `stealth_browser` MCP service via `mcporter`

---

## Recommended Approach

### Option A: Thin `mcporter` subprocess adapter inside `ScreenshotCapture` (Recommended)
- Keep `ScreenshotCapture` synchronous and shell out to `mcporter call stealth_browser.*`.
- Pros: smallest repo change, matches the user’s deployment model, no new HTTP client layer.
- Cons: requires careful quoting/JSON handling and mocked subprocess unit tests.

### Option B: Direct HTTP client for stealth_browser
- Replace `mcporter` with direct HTTP requests to the MCP bridge.
- Pros: easier to test at the protocol layer and avoids shell quoting.
- Cons: not aligned with the provided contract, adds infra assumptions, increases scope.

### Option C: External screenshot generation script only, no preset integration
- Keep screenshots entirely outside the preset and let operators compose manually.
- Pros: smallest code footprint.
- Cons: misses the main goal of automatic hybrid generation and leaves layout logic duplicated.

**Recommendation:** Use Option A. It matches the stated environment, keeps the preset clean, and limits risk to one adapter boundary.

## Key Decisions To Lock Before Coding

1. Treat `StockAnalysisPreset(..., screenshot_path=None)` as the stable default and never change existing SVG-only output when no screenshot is supplied.
2. Make the screenshot capture utility responsible for browser lifecycle and always close the instance in `finally`.
3. Prefer an explicit wait/poll helper over raw `time.sleep()`; only use bounded fallback sleep if the actual `stealth_browser` tool surface lacks a wait primitive.
4. Keep screenshots and generated PNGs out of git; add ignore rules for `screenshot_*.png`.
5. Make the “one-shot” helper actually compose, or rename it. Recommended: keep the requested filename `capture_and_compose.py` and require a data file input for real composition.

## Preflight

### Task 0: Validate the external screenshot contract before implementation

**Files:**
- Modify: `.gitignore`
- Inspect only: `blog/tools/svg_converter.py`
- Inspect only: local `mcporter` / `stealth_browser` tool list

**Step 1: Verify the real `stealth_browser` tool names and arguments**

Run:

```bash
mcporter list
mcporter schema stealth_browser
```

Expected:
- Confirm whether the real methods are named exactly `spawn_browser`, `navigate`, `take_screenshot`, `close_instance`, and whether a wait helper exists.

**Step 2: Add ignore patterns for screenshot artifacts**

Add:

```gitignore
screenshot_*.png
blog/images/screenshot_*.png
```

**Step 3: Re-run a clean status check**

Run:

```bash
git status --short
```

Expected:
- Only the planned source/doc changes appear; no captured screenshots should be tracked.

**Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: ignore generated chart screenshots"
```

## Implementation

### Task 1: Lock `ImageComposer` behavior with unit tests

**Files:**
- Create: `blog/tools/image_composer.py`
- Create: `blog/tests/test_screenshot.py`

**Step 1: Write failing unit tests for PNG embedding**

Cover:
- existing PNG file embeds as `data:image/png;base64,...`
- `clipPath` is emitted with deterministic `id`
- border/shadow toggles behave as expected
- missing PNG returns an SVG comment instead of raising
- hybrid technical layout includes screenshot, indicator fragment, and support/resistance fragment

Suggested commands:

```bash
uv run pytest blog/tests/test_screenshot.py -q
```

Expected:
- Fail because `blog.tools.image_composer` does not exist yet.

**Step 2: Implement `ImageComposer` minimally**

Implementation notes:
- Read PNG bytes from disk and base64 encode with ASCII output.
- Keep output as SVG fragments only; no file I/O in the composer.
- Generate unique clip IDs from position and size, not from random state.
- Keep the layout wrapper in `create_hybrid_technical(...)` and reuse `SVGComponent.header/background/title/footer`.

**Step 3: Re-run tests**

Run:

```bash
uv run pytest blog/tests/test_screenshot.py::TestImageComposer -q
```

Expected:
- Pass.

**Step 4: Commit**

```bash
git add blog/tools/image_composer.py blog/tests/test_screenshot.py
git commit -m "feat: add screenshot image composer for hybrid svg layouts"
```

### Task 2: Add hybrid rendering to `StockAnalysisPreset`

**Files:**
- Modify: `blog/tools/presets/stock_analysis.py`
- Modify: `blog/tests/test_stock_preset.py`

**Step 1: Write failing preset tests**

Add tests for:
- `screenshot_path=None` keeps existing technical SVG behavior
- valid `screenshot_path` switches the technical image to hybrid mode
- hybrid technical SVG contains embedded PNG data and still contains indicator/support-resistance fragments
- `generate_pngs()` deletes only the oversized hybrid technical SVG after successful PNG conversion, while leaving the PNG output intact

Run:

```bash
uv run pytest blog/tests/test_stock_preset.py -q
```

Expected:
- Fail because `StockAnalysisPreset` does not accept `screenshot_path` and no cleanup behavior exists yet.

**Step 2: Implement the preset branch**

Implementation notes:
- Extend `__init__` with `screenshot_path: Path | None = None`.
- In `_create_technical()`, branch:
  - no screenshot: keep current `PriceChart`-based path untouched
  - screenshot present and exists: call `ImageComposer.create_hybrid_technical(...)`
- Keep all other image creators unchanged.

**Step 3: Implement post-conversion cleanup**

Implementation notes:
- After `SVGConverter.convert_all(...)` succeeds in `generate_pngs()`, remove the technical SVG only when hybrid mode was used.
- Do not delete the SVG before PNG conversion completes.
- Do not delete the other 4 SVGs unless explicitly requested in a future phase.

**Step 4: Re-run targeted tests**

Run:

```bash
uv run pytest blog/tests/test_stock_preset.py blog/tests/test_screenshot.py::TestImageComposer -q
```

Expected:
- Pass.

**Step 5: Commit**

```bash
git add blog/tools/presets/stock_analysis.py blog/tests/test_stock_preset.py blog/tests/test_screenshot.py
git commit -m "feat: add hybrid screenshot mode to stock analysis preset"
```

### Task 3: Add `ScreenshotCapture` with mocked unit tests and gated integration tests

**Files:**
- Create: `blog/tools/screenshot_capture.py`
- Modify: `blog/tests/test_screenshot.py`

**Step 1: Write failing unit tests around the subprocess boundary**

Add tests for:
- `_mcporter_call()` invokes `subprocess.run(...)` with timeout and parses JSON
- `_ensure_browser()` caches the browser instance ID
- `close()` calls the close method once and clears the cached ID
- `capture_tradingview()` builds the expected TradingView embed URL and returns a PNG path
- `capture_upbit_chart()` hits the expected URLs in sequence

Use monkeypatch/mocks so these tests do not require `stealth_browser`.

**Step 2: Keep or refine the gated integration tests**

Integration coverage should remain skip-gated behind a sentinel such as:

```python
pytest.mark.skipif(
    not Path("/tmp/.stealth_browser_running").exists(),
    reason="stealth_browser 서비스 미실행",
)
```

If the environment already exposes a better health-check probe, prefer that over a sentinel file.

**Step 3: Implement `ScreenshotCapture`**

Implementation notes:
- Keep all file writes under `blog/images` by default.
- Avoid raw shell-string interpolation for JSON-like arguments if the real `mcporter` CLI supports structured payloads; otherwise quote safely and keep the adapter private.
- Prefer a dedicated `_wait_for_chart_ready(...)` helper over scattered sleeps.
- Do not allow more than one active browser per instance.

**Step 4: Re-run tests**

Run:

```bash
uv run pytest blog/tests/test_screenshot.py -q
```

Expected:
- Unit tests pass locally.
- Integration tests skip cleanly when `stealth_browser` is unavailable.

**Step 5: Manual smoke check**

Run:

```bash
uv run python - <<'PY'
from pathlib import Path
from blog.tools.screenshot_capture import ScreenshotCapture

capture = ScreenshotCapture(output_dir=Path("blog/images"))
try:
    path = capture.capture_tradingview("BINANCE:BTCUSDT", interval="D", theme="dark")
    print(path, path.exists(), path.stat().st_size)
finally:
    capture.close()
PY
```

Expected:
- PNG exists and is materially larger than an empty image.

**Step 6: Commit**

```bash
git add blog/tools/screenshot_capture.py blog/tests/test_screenshot.py
git commit -m "feat: add stealth browser screenshot capture utility"
```

### Task 4: Add a real capture-and-compose CLI

**Files:**
- Create: `blog/tools/capture_and_compose.py`
- Modify: `blog/tools/__init__.py`

**Step 1: Write the CLI contract first**

Recommended CLI:

```bash
uv run python blog/tools/capture_and_compose.py BINANCE:BTCUSDT \
  --interval D \
  --theme dark \
  --data-json /path/to/analysis.json \
  --output-dir blog/images
```

Why `--data-json` is recommended:
- Without analysis data the script can capture, but it cannot truthfully “compose” a full technical image using the preset.
- This avoids a misleading helper that only prints instructions.

**Step 2: Implement minimal composition flow**

Flow:
- capture screenshot
- load analysis JSON
- instantiate `StockAnalysisPreset(..., screenshot_path=...)`
- call `generate_svgs()` or `generate_pngs()`
- print output paths
- always `close()` the browser in `finally`

**Step 3: Export new top-level helpers only if useful**

Update `blog/tools/__init__.py` only if callers benefit from:
- `ScreenshotCapture`
- `ImageComposer`

Do not bloat `__all__` if the project usually imports these modules directly.

**Step 4: Re-run smoke test**

Run:

```bash
uv run python blog/tools/capture_and_compose.py BINANCE:BTCUSDT --help
```

Expected:
- CLI usage includes `--data-json`.

**Step 5: Commit**

```bash
git add blog/tools/capture_and_compose.py blog/tools/__init__.py
git commit -m "feat: add hybrid chart capture and compose cli"
```

### Task 5: Finish exports and regression coverage

**Files:**
- Modify: `blog/tools/stock/__init__.py` only if exports are incomplete
- Modify: `blog/tests/test_screenshot.py`
- Modify: `blog/tests/test_stock_preset.py`

**Step 1: Verify stock exports**

Current expectation:
- `CandlestickChart`
- `VolumeProfile`

If already present, leave `blog/tools/stock/__init__.py` unchanged.

**Step 2: Add a regression test for backwards compatibility**

Cover:
- existing users can still instantiate `StockAnalysisPreset(symbol, data)`
- output remains 5 files
- technical SVG still contains vector chart elements when no screenshot is passed

**Step 3: Run the focused verification suite**

Run:

```bash
uv run pytest blog/tests/test_screenshot.py blog/tests/test_stock_preset.py blog/tests/test_stock_components.py -q
uv run ruff check blog/tools blog/tests
```

Expected:
- All targeted tests pass.
- Ruff is clean for new files.

**Step 4: Commit**

```bash
git add blog/tests/test_screenshot.py blog/tests/test_stock_preset.py
git commit -m "test: lock hybrid screenshot and preset compatibility contracts"
```

## Final Verification

### Task 6: End-to-end proof with one real screenshot

**Files:**
- No new files required

**Step 1: Capture and render one technical image**

Run:

```bash
uv run python blog/tools/capture_and_compose.py BINANCE:BTCUSDT \
  --interval D \
  --theme dark \
  --data-json /tmp/btc-analysis.json \
  --output-dir blog/images
```

Expected:
- screenshot PNG is created
- hybrid technical SVG is generated
- corresponding PNG is generated
- temporary oversized hybrid technical SVG is removed if `generate_pngs()` path is used

**Step 2: Spot-check the produced PNG**

Check:
- left chart region is a real screenshot, not vector candles
- right indicator panel and bottom support/resistance remain SVG-styled
- no clipping, stretched aspect ratio, or unreadable borders

**Step 3: Run final verification commands**

```bash
uv run pytest blog/tests/test_screenshot.py blog/tests/test_stock_preset.py -q
uv run ruff check blog/tools blog/tests
git status --short
```

Expected:
- tests pass
- lint passes
- no generated screenshots are staged

**Step 4: Commit**

```bash
git add blog/tools blog/tests .gitignore
git commit -m "feat: add hybrid real-chart screenshot rendering for blog images"
```

## Notes For The Implementer

- The largest technical risk is assuming the `stealth_browser` CLI contract. Validate that first.
- The most likely regression is breaking SVG-only output in `_create_technical()`. Guard it with tests before editing.
- Keep `ImageComposer` pure and deterministic; keep all side effects inside `ScreenshotCapture` and CLI code.
- If the real `mcporter` surface does not provide a proper wait primitive, add a small bounded polling loop in one place instead of repeated `sleep()` calls.
- If the “delete temporary hybrid SVG after PNG conversion” behavior proves too invasive, gate it behind a keyword argument with default enabled only for hybrid mode.
