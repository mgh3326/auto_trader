# Blog Image Script Migration — Phase 2

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate existing blog image generation scripts to compose Phase 1 components (`blog/tools/components/` and `blog/tools/stock/`) instead of hardcoding SVG, achieving ~50% line count reduction while maintaining identical visual output.

**Architecture:** Each script method is converted from raw SVG string templates to component composition: `SVGComponent.header/background/title/footer` for document structure + domain components (`FlowDiagram`, `BarChart`, etc.) for content. Methods that are too UI-specific for generic components use SVGComponent helpers with structured Python data instead of inline SVG.

**Tech Stack:** Python 3.13+, pytest, existing `blog/tools/components/` and `blog/tools/stock/` packages, `SVGComponent` base helpers.

---

## Pre-Implementation Context

**Phase 1 Component Inventory (already built):**

| Component | File | API Pattern | Use Case |
|-----------|------|-------------|----------|
| `SVGComponent` | `components/base.py` | `.header()`, `.footer()`, `.background()`, `.title()` | Document structure |
| `ThumbnailTemplate` | `components/thumbnail.py` | `.create(title_line1, title_line2, icons, ...)` | Blog thumbnails |
| `InfoCard` | `components/card.py` | `.create(x, y, w, h, title, value, ...)` | KPI/metric cards |
| `BarChart` | `components/bar_chart.py` | `.create(x, y, w, h, data, direction, ...)` | Vertical/horizontal bars |
| `ComparisonTable` | `components/table.py` | `.create(x, y, w, h, headers, rows, ...)` | Data tables |
| `EventTimeline` | `components/timeline.py` | `.create(x, y, w, h, events, ...)` | Point-event timelines |
| `FlowDiagram` | `components/flow_diagram.py` | `.create(nodes, edges)` | Architecture box-arrow diagrams |
| `CodeBlock` | `components/code_block.py` | `.create(x, y, w, h, code, ...)` | Code snippet display |

**Migration Target Inventory:**

| Script | Lines | Methods | Thumbnail Uses Components? | Diagram Methods |
|--------|-------|---------|---------------------------|-----------------|
| `python314_images.py` | 198 | 3 | Yes (ThumbnailTemplate) | `create_version_timeline`, `create_performance_comparison` |
| `mcp_server_images.py` | 308 | 3 | Yes | `create_architecture`, `create_routing` |
| `kis_trading_images.py` | 642 | 7 | Yes | `create_architecture`, `create_buy_flow`, `create_erd`, `create_dashboard`, `create_progress`, `create_flower` |
| `openclaw_images.py` | 894 | 4 | Yes | `create_architecture`, `create_ssh_tunnel`, `create_auth_flow` |

**Key Fact:** `samsung_analysis_images.py` does NOT exist in the codebase. The Phase 1 `StockAnalysisPreset` already produces the 5-image stock analysis set. Task 1 creates a thin wrapper script for it.

**Component Fit Assessment:**
- **Exact fit:** `FlowDiagram` replaces most architecture/flow/routing diagrams (box-arrow patterns)
- **Structural fit:** `SVGComponent.header/background/title/footer` eliminates ~20-30 lines of boilerplate per method
- **Partial fit:** `BarChart` handles simple comparisons; paired/grouped bars need custom composition
- **No fit:** `EventTimeline` is point-markers (not Gantt spans); dashboard mockups are too UI-specific. These use SVGComponent helpers + structured data instead of component drop-in.

**Migration Strategy per Method Type:**

| Pattern in SVG | Strategy | Reduction |
|---|---|---|
| SVG header/background/title/footer boilerplate | Replace with `SVGComponent.*` | ~25 lines/method |
| Box-and-arrow architecture diagrams | Replace with `FlowDiagram.create()` | 60-80% of method |
| Simple bar comparisons | Replace with `BarChart.create()` | 40-60% of method |
| Table structures (ERD, comparison) | Replace with `ComparisonTable.create()` | 50-70% of method |
| Dashboard/progress/flower mockups | Keep custom, use `SVGComponent` helpers only | 20-30% |
| Gantt-style timelines | Keep custom, use `SVGComponent` helpers | 20-30% |

**Conventions (MUST follow):**
- All `create_thumbnail()` methods already use `ThumbnailTemplate` — do NOT touch them.
- `blog/` is NOT a Python package (no `blog/__init__.py`). Scripts use `sys.path.insert(0, ...)`.
- SVG font: `font-family="Arial, sans-serif"` (Playwright applies Noto Sans KR at render time).
- Components return SVG fragment strings. Wrap with `SVGComponent.header(w, h)` / `SVGComponent.footer()`.
- Existing `BlogImageGenerator` static helpers (`rect`, `text`, `line`, `circle`) remain available — prefer components where possible but don't force-fit.
- `openclaw_images.py` is the only script using `BlogImageGenerator` helpers extensively (122 calls of `self.rect/text/line/circle`).

**Expected Outcomes:**

| Script | Before | After (est.) | Reduction |
|--------|--------|-------------|-----------|
| `python314_images.py` | 198 | ~120 | 40% |
| `mcp_server_images.py` | 308 | ~130 | 58% |
| `kis_trading_images.py` | 642 | ~320 | 50% |
| `openclaw_images.py` | 894 | ~380 | 57% |

---

## Task 1: Create `samsung_analysis_images.py` (reference wrapper)

**Files:**
- Create: `blog/images/samsung_analysis_images.py`
- Modify: `blog/tests/test_stock_preset.py` (add wrapper test)

**Step 1: Write the failing test**

Add to `blog/tests/test_stock_preset.py`:

```python
class TestSamsungAnalysisImages:
    """Tests for the SamsungAnalysisImages wrapper script."""

    def test_import_and_instantiate(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        gen = SamsungAnalysisImages()
        assert gen.prefix == "samsung_analysis"
        assert hasattr(gen, "get_images")

    def test_get_images_returns_five(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        gen = SamsungAnalysisImages()
        images = gen.get_images()
        assert len(images) == 5
        names = [name for name, _, _, _ in images]
        assert "thumbnail" in names
        assert "technical" in names

    def test_default_data_populated(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        gen = SamsungAnalysisImages()
        assert gen.data["company_profile"]["name"] == "삼성전자"
        assert "indicators" in gen.data
        assert "valuation" in gen.data

    def test_custom_data_override(self) -> None:
        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        custom = {"company_profile": {"name": "SK하이닉스", "symbol": "000660", "sector": "반도체"}}
        gen = SamsungAnalysisImages(data=custom)
        assert gen.data["company_profile"]["name"] == "SK하이닉스"

    def test_generate_svgs_produces_valid_files(self) -> None:
        import tempfile
        from pathlib import Path

        from blog.images.samsung_analysis_images import SamsungAnalysisImages

        with tempfile.TemporaryDirectory() as tmpdir:
            gen = SamsungAnalysisImages(images_dir=Path(tmpdir))
            paths = gen.generate_svgs()
            assert len(paths) == 5
            for p in paths:
                assert p.exists()
                content = p.read_text()
                assert content.startswith("<?xml")
                assert "</svg>" in content
```

**Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest blog/tests/test_stock_preset.py::TestSamsungAnalysisImages -v --no-header
```

Expected: FAIL — `blog.images.samsung_analysis_images` does not exist.

**Step 3: Implement `samsung_analysis_images.py`**

```python
#!/usr/bin/env python3
"""삼성전자 종목 분석 블로그 이미지 생성기.

StockAnalysisPreset을 BlogImageGenerator 인터페이스로 래핑합니다.
컴포넌트 시스템을 사용하여 5장의 분석 이미지를 생성합니다.

사용법:
    python blog/images/samsung_analysis_images.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.image_generator import BlogImageGenerator
from blog.tools.presets.stock_analysis import StockAnalysisPreset


class SamsungAnalysisImages(BlogImageGenerator):
    """삼성전자 종합 분석 이미지 — StockAnalysisPreset 래퍼.

    Phase 1 컴포넌트 시스템의 레퍼런스 구현입니다.
    data 파라미터를 전달하면 커스텀 데이터로 이미지를 생성할 수 있습니다.
    """

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        images_dir: Path | None = None,
    ) -> None:
        super().__init__("samsung_analysis", images_dir)
        self.data = data or self._default_data()
        self._preset = StockAnalysisPreset(
            symbol="005930",
            data=self.data,
            output_dir=self.images_dir,
        )

    def get_images(self) -> list[tuple[str, int, int, Callable[[], str]]]:
        return [
            ("thumbnail", 1200, 630, self._preset._create_thumbnail),
            ("technical", 1400, 800, self._preset._create_technical),
            ("fundamental", 1400, 800, self._preset._create_fundamental),
            ("supply_demand", 1400, 800, self._preset._create_supply_demand),
            ("conclusion", 1400, 800, self._preset._create_conclusion),
        ]

    @staticmethod
    def _default_data() -> dict[str, Any]:
        """하드코딩 기본 데이터 (하위호환용)."""
        return {
            "company_profile": {
                "name": "삼성전자",
                "symbol": "005930",
                "sector": "반도체",
            },
            "indicators": {
                "rsi14": 57.16,
                "macd_histogram": -527,
                "macd_signal": "매도 신호",
                "adx": 16.37,
                "plus_di": 22.5,
                "minus_di": 18.3,
                "stoch_rsi_k": 0.78,
                "stoch_rsi_d": 0.65,
            },
            "valuation": {
                "per": 30.38,
                "pbr": 1.82,
                "roe": 6.01,
                "consensus_target": 85000,
                "current_price": 65800,
            },
            "financials": {
                "annual_earnings": [
                    {"year": "2021", "operating_income": 51_633_000_000_000},
                    {"year": "2022", "operating_income": 43_376_000_000_000},
                    {"year": "2023", "operating_income": 6_567_000_000_000},
                    {"year": "2024E", "operating_income": 32_700_000_000_000},
                ],
                "quarterly_margins": [
                    {"quarter": "Q1", "margin": 0.05},
                    {"quarter": "Q2", "margin": 0.08},
                    {"quarter": "Q3", "margin": 0.12},
                    {"quarter": "Q4E", "margin": 0.15},
                ],
            },
            "investor_trends": {
                "foreign_net": -15234,
                "institution_net": 8721,
                "individual_net": 6513,
                "foreign_consecutive_sell_days": 5,
            },
            "investment_opinions": {
                "opinions": [
                    {"firm": "삼성증권", "rating": "매수", "target": 90000},
                    {"firm": "NH투자", "rating": "매수", "target": 85000},
                    {"firm": "미래에셋", "rating": "중립", "target": 70000},
                ],
                "consensus": {"rating": "매수", "avg_target": 82000},
            },
            "support_resistance": {
                "supports": [62000, 58000, 55000],
                "resistances": [68000, 72000, 75000],
                "current_price": 65800,
            },
            "sector_peers": [
                {"name": "삼성전자", "market_cap": "350조", "per": "30.38"},
                {"name": "SK하이닉스", "market_cap": "140조", "per": "25.12"},
            ],
            "ohlcv": [
                {
                    "date": f"2024-01-{d:02d}",
                    "open": 65000 + d * 100,
                    "high": 66000 + d * 100,
                    "low": 64000 + d * 100,
                    "close": 65500 + d * 100,
                    "volume": 10000000 + d * 100000,
                }
                for d in range(2, 22)
            ],
        }


if __name__ == "__main__":
    SamsungAnalysisImages().generate()
```

**Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest blog/tests/test_stock_preset.py -v --no-header
```

Expected: All tests PASS.

**Step 5: Lint**

```bash
uv run ruff check blog/images/samsung_analysis_images.py && uv run ruff format --check blog/images/samsung_analysis_images.py
```

**Step 6: Commit**

```bash
git add blog/images/samsung_analysis_images.py blog/tests/test_stock_preset.py
git commit -m "feat(blog): add samsung_analysis_images.py — StockAnalysisPreset wrapper with default data"
```

---

## Task 2: Migrate `python314_images.py` — version timeline

**Files:**
- Modify: `blog/images/python314_images.py` (replace `create_version_timeline`)
- Create: `blog/tests/test_image_migration.py` (migration regression tests)

**Step 1: Write the regression test**

```python
# blog/tests/test_image_migration.py
"""Regression tests for blog image script migration.

Each test verifies that migrated methods produce valid SVGs
containing expected content elements.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestPython314Migration:
    """Regression tests for python314_images.py migration."""

    def test_version_timeline_valid_svg(self) -> None:
        from blog.images.python314_images import Python314Images

        gen = Python314Images("python314_upgrade")
        svg = gen.create_version_timeline()
        assert svg.startswith("<?xml")
        assert "</svg>" in svg

    def test_version_timeline_content(self) -> None:
        from blog.images.python314_images import Python314Images

        gen = Python314Images("python314_upgrade")
        svg = gen.create_version_timeline()
        # Must contain all Python versions
        assert "Python 3.11" in svg
        assert "Python 3.12" in svg
        assert "Python 3.13" in svg
        assert "Python 3.14" in svg
        # Must contain title
        assert "타임라인" in svg
        # Must have timeline dimensions
        assert 'width="1200"' in svg
        assert 'height="450"' in svg

    def test_performance_comparison_valid_svg(self) -> None:
        from blog.images.python314_images import Python314Images

        gen = Python314Images("python314_upgrade")
        svg = gen.create_performance_comparison()
        assert svg.startswith("<?xml")
        assert "</svg>" in svg

    def test_performance_comparison_content(self) -> None:
        from blog.images.python314_images import Python314Images

        gen = Python314Images("python314_upgrade")
        svg = gen.create_performance_comparison()
        # Must contain comparison categories
        assert "3.13" in svg
        assert "3.14" in svg
        assert "성능" in svg
        assert 'width="1200"' in svg

    def test_thumbnail_unchanged(self) -> None:
        from blog.images.python314_images import Python314Images

        gen = Python314Images("python314_upgrade")
        svg = gen.create_thumbnail()
        assert "Python 3.14" in svg
        assert 'width="1200"' in svg
        assert 'height="630"' in svg
```

**Step 2: Run tests to verify they PASS against current code (pre-migration baseline)**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestPython314Migration -v --no-header
```

Expected: All PASS — this establishes the baseline.

**Step 3: Replace `create_version_timeline` with component composition**

The version timeline uses Gantt-style span bars that `EventTimeline` cannot represent. Strategy: use `SVGComponent` helpers for structure + structured data loops for the bars.

Replace the `create_version_timeline` method in `blog/images/python314_images.py`:

```python
    def create_version_timeline(self) -> str:
        """Python 버전 타임라인 (1200x450)"""
        from blog.tools.components.base import SVGComponent

        W, H = 1200, 450
        svg = SVGComponent.header(W, H)
        svg += SVGComponent.background(W, H, fill="#ffffff")
        svg += SVGComponent.title(W, "Python 버전별 릴리즈 및 EOL 타임라인")

        # Timeline axis
        svg += '    <line x1="100" y1="350" x2="1100" y2="350" stroke="#666666" stroke-width="3"/>\n'

        # Year labels and tick marks
        years = [("150", "2022"), ("300", "2023"), ("450", "2024"), ("600", "2025"),
                 ("750", "2026"), ("900", "2027"), ("1050", "2028+")]
        for x_str, year in years:
            svg += f'    <text x="{x_str}" y="380" font-family="Arial, sans-serif" font-size="14" fill="#666666" text-anchor="middle">{year}</text>\n'
            svg += f'    <line x1="{x_str}" y1="340" x2="{x_str}" y2="360" stroke="#999999" stroke-width="2"/>\n'

        # Version span bars: (x, y, width, height, fill, stroke, label, date_text, eol_marker)
        versions = [
            (150, 90, 600, 35, "#FFCDD2", "#E57373", "Python 3.11", "2022.10 Release → 2027.10 EOL", (750, "#E57373", "#C62828")),
            (300, 140, 750, 35, "#FFE0B2", "#FFB74D", "Python 3.12", "2023.10 Release → 2028.10 EOL", (1050, "#FFB74D", "#EF6C00")),
            (450, 190, 650, 35, "#C8E6C9", "#81C784", "Python 3.13", "2024.10 Release → 2029.10 EOL", None),
        ]
        for bx, by, bw, bh, fill, stroke, label, date_text, eol in versions:
            svg += f'    <rect x="{bx}" y="{by}" width="{bw}" height="{bh}" fill="{fill}" stroke="{stroke}" stroke-width="2" rx="5"/>\n'
            label_color = stroke.replace("#E5", "#C6").replace("#FFB", "#EF6").replace("#81C", "#2E7") if "#" in stroke else "#333333"
            svg += f'    <text x="{bx + 20}" y="{by + 25}" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="{label_color}">{label}</text>\n'
            svg += f'    <text x="{bx + bw // 2}" y="{by + 25}" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="middle">{date_text}</text>\n'
            if eol:
                ex, ec, tc = eol
                svg += f'    <circle cx="{ex}" cy="{by + bh // 2}" r="8" fill="{ec}"/>\n'
                svg += f'    <text x="{ex + 15}" y="{by + bh // 2 + 5}" font-family="Arial, sans-serif" font-size="11" fill="{tc}">EOL</text>\n'

        # Python 3.14 — highlighted
        svg += '    <rect x="600" y="240" width="500" height="45" fill="#FFD43B" stroke="#3776AB" stroke-width="3" rx="5"/>\n'
        svg += '    <text x="620" y="270" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#3776AB">Python 3.14</text>\n'
        svg += '    <text x="750" y="270" font-family="Arial, sans-serif" font-size="14" fill="#333333">"Pi Release"</text>\n'
        svg += '    <text x="900" y="270" font-family="Arial, sans-serif" font-size="12" fill="#666666">2025.10 → 2030.10</text>\n'

        # Current time marker
        svg += '    <line x1="650" y1="80" x2="650" y2="350" stroke="#F44336" stroke-width="2" stroke-dasharray="5,5"/>\n'
        svg += '    <text x="650" y="410" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#F44336" text-anchor="middle">현재 (2025.12)</text>\n'

        # Legend
        svg += '    <rect x="100" y="400" width="20" height="15" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>\n'
        svg += '    <text x="130" y="412" font-family="Arial, sans-serif" font-size="12" fill="#333333">현재 사용 중</text>\n'
        svg += '    <circle cx="250" cy="408" r="6" fill="#E57373"/>\n'
        svg += '    <text x="265" y="412" font-family="Arial, sans-serif" font-size="12" fill="#666666">EOL (End of Life)</text>\n'

        svg += SVGComponent.footer()
        return svg
```

**Step 4: Replace `create_performance_comparison` with component composition**

```python
    def create_performance_comparison(self) -> str:
        """성능 비교 그래프 (1200x500)"""
        from blog.tools.components.base import SVGComponent

        W, H = 1200, 500
        svg = SVGComponent.header(W, H)
        svg += SVGComponent.background(W, H, fill="#ffffff")
        svg += SVGComponent.title(W, "Python 3.13 vs 3.14 성능 비교", y=40, font_size=24)

        # Axis
        svg += '    <line x1="150" y1="80" x2="150" y2="400" stroke="#666666" stroke-width="2"/>\n'
        svg += '    <line x1="150" y1="400" x2="1100" y2="400" stroke="#666666" stroke-width="2"/>\n'

        # Y-axis labels and grid
        y_labels = [("90", "100%"), ("170", "80%"), ("250", "60%"), ("330", "40%"), ("400", "20%")]
        for y_pos, label in y_labels:
            svg += f'    <text x="140" y="{y_pos}" font-family="Arial, sans-serif" font-size="12" fill="#666666" text-anchor="end">{label}</text>\n'
            svg += f'    <line x1="150" y1="{y_pos}" x2="1100" y2="{y_pos}" stroke="#e0e0e0" stroke-width="1"/>\n'

        # Paired bars: (center_x, category, v313_label, v314_label, delta)
        categories = [
            (285, "앱 시작 시간", "2.298s", "2.156s", "-6%"),
            (515, "테스트 실행", "17.91s", "17.12s", "-4%"),
            (745, "API 응답 시간", "78.67ms", "75.23ms", "-4%"),
            (975, "메모리 사용량", "138.7MB", "135.2MB", "-2.5%"),
        ]
        for cx, cat_label, v313, v314, delta in categories:
            # 3.13 bar (full height reference)
            x313 = cx - 90
            svg += f'    <rect x="{x313}" y="90" width="80" height="310" fill="#81C784" stroke="#388E3C" stroke-width="2"/>\n'
            svg += f'    <text x="{x313 + 40}" y="85" font-family="Arial, sans-serif" font-size="11" fill="#2E7D32" text-anchor="middle">{v313}</text>\n'

            # 3.14 bar (slightly shorter — varies by delta, but uniform for visual simplicity)
            x314 = cx
            bar_offset = int(abs(float(delta.strip("-%"))) * 3.1)  # ~proportional
            svg += f'    <rect x="{x314}" y="{90 + bar_offset}" width="80" height="{310 - bar_offset}" fill="#FFD43B" stroke="#3776AB" stroke-width="2"/>\n'
            svg += f'    <text x="{x314 + 40}" y="{85 + bar_offset}" font-family="Arial, sans-serif" font-size="11" fill="#3776AB" text-anchor="middle">{v314}</text>\n'
            svg += f'    <text x="{x314 + 40}" y="{107 + bar_offset}" font-family="Arial, sans-serif" font-size="11" font-weight="bold" fill="#4CAF50" text-anchor="middle">{delta}</text>\n'

            # Category label
            svg += f'    <text x="{cx}" y="430" font-family="Arial, sans-serif" font-size="13" fill="#333333" text-anchor="middle">{cat_label}</text>\n'

        # Legend
        svg += '    <rect x="400" y="455" width="25" height="18" fill="#81C784" stroke="#388E3C" stroke-width="1"/>\n'
        svg += '    <text x="435" y="470" font-family="Arial, sans-serif" font-size="14" fill="#333333">Python 3.13</text>\n'
        svg += '    <rect x="560" y="455" width="25" height="18" fill="#FFD43B" stroke="#3776AB" stroke-width="1"/>\n'
        svg += '    <text x="595" y="470" font-family="Arial, sans-serif" font-size="14" fill="#333333">Python 3.14</text>\n'
        svg += '    <text x="850" y="470" font-family="Arial, sans-serif" font-size="14" font-weight="bold" fill="#4CAF50">평균 4% 성능 향상</text>\n'

        svg += SVGComponent.footer()
        return svg
```

**Step 5: Remove the `sys.path.insert` block if already present via parent, and add `SVGComponent` import at top**

Update the imports at top of file:

```python
#!/usr/bin/env python3
"""
Python 3.14 업그레이드 블로그 이미지 생성기

사용법:
    uv run python blog/images/python314_images.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from blog.tools.image_generator import BlogImageGenerator, ThumbnailTemplate  # noqa: E402
```

(Keep as-is. The `SVGComponent` import is done locally inside each method to avoid circular import issues with the `sys.path` pattern.)

**Step 6: Run regression tests**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestPython314Migration -v --no-header
```

Expected: All PASS.

**Step 7: Visual verification**

```bash
uv run python -c "
import sys; sys.path.insert(0, '.')
from blog.images.python314_images import Python314Images
gen = Python314Images('python314_upgrade')
paths = gen.generate_svgs()
for p in paths:
    print(f'  {p.name} ({p.stat().st_size:,} bytes)')
"
```

Open the generated SVG files in a browser to visually confirm they match the original.

**Step 8: Lint and commit**

```bash
uv run ruff check blog/images/python314_images.py && uv run ruff format --check blog/images/python314_images.py
git add blog/images/python314_images.py blog/tests/test_image_migration.py
git commit -m "refactor(blog): migrate python314_images.py to SVGComponent composition"
```

---

## Task 3: Migrate `mcp_server_images.py` — architecture + routing

**Files:**
- Modify: `blog/images/mcp_server_images.py`
- Modify: `blog/tests/test_image_migration.py` (add MCP regression tests)

**Step 1: Write regression tests**

Add to `blog/tests/test_image_migration.py`:

```python
class TestMCPServerMigration:
    """Regression tests for mcp_server_images.py migration."""

    def test_architecture_valid_svg(self) -> None:
        from blog.images.mcp_server_images import MCPServerImages

        gen = MCPServerImages("mcp_server")
        svg = gen.create_architecture()
        assert svg.startswith("<?xml")
        assert "</svg>" in svg
        assert 'width="1400"' in svg

    def test_architecture_content(self) -> None:
        from blog.images.mcp_server_images import MCPServerImages

        gen = MCPServerImages("mcp_server")
        svg = gen.create_architecture()
        assert "Claude" in svg or "MCP" in svg
        assert "FastMCP" in svg or "Server" in svg

    def test_routing_valid_svg(self) -> None:
        from blog.images.mcp_server_images import MCPServerImages

        gen = MCPServerImages("mcp_server")
        svg = gen.create_routing()
        assert svg.startswith("<?xml")
        assert "</svg>" in svg
        assert 'width="1200"' in svg

    def test_routing_content(self) -> None:
        from blog.images.mcp_server_images import MCPServerImages

        gen = MCPServerImages("mcp_server")
        svg = gen.create_routing()
        # Routing diagram should mention symbol/market routing concepts
        assert isinstance(svg, str)
        assert len(svg) > 500

    def test_thumbnail_unchanged(self) -> None:
        from blog.images.mcp_server_images import MCPServerImages

        gen = MCPServerImages("mcp_server")
        svg = gen.create_thumbnail()
        assert "MCP" in svg
        assert 'width="1200"' in svg
```

**Step 2: Run baseline tests**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestMCPServerMigration -v --no-header
```

Expected: All PASS.

**Step 3: Migrate `create_architecture`**

Read the full method first, then rewrite using `SVGComponent.header/background/title/footer` + `FlowDiagram.create()` for the node-edge layout. Architecture diagrams typically have:
- Layer headers ("Client Layer", "Server Layer", "Data Layer")
- Boxes for services/components
- Arrows for connections

The approach:
1. Use `SVGComponent` for document structure
2. Use text elements for layer headers (section labels)
3. Use `FlowDiagram.create()` for the boxes and arrows within each layer
4. Keep layer background rects as simple inline SVG

**Step 4: Migrate `create_routing`**

Same pattern as architecture — `FlowDiagram` for the decision tree / routing diagram.

**Step 5: Run regression tests, visual verification, lint, commit**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestMCPServerMigration -v --no-header
uv run ruff check blog/images/mcp_server_images.py && uv run ruff format --check blog/images/mcp_server_images.py
git add blog/images/mcp_server_images.py blog/tests/test_image_migration.py
git commit -m "refactor(blog): migrate mcp_server_images.py to FlowDiagram + SVGComponent"
```

---

## Task 4: Migrate `kis_trading_images.py` — architecture, buy flow, ERD

**Files:**
- Modify: `blog/images/kis_trading_images.py`
- Modify: `blog/tests/test_image_migration.py`

**Step 1: Write regression tests**

Add `TestKISTradingMigration` class covering all 7 methods — same pattern as above (valid SVG + key content assertions).

**Step 2: Run baseline**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestKISTradingMigration -v --no-header
```

**Step 3: Migrate methods (3 migrable + 3 partial + 1 skip)**

Migration plan per method:

| Method | Lines | Strategy | Component |
|--------|-------|----------|-----------|
| `create_thumbnail` | 32-47 | **SKIP** — already uses ThumbnailTemplate | — |
| `create_architecture` | 49-160 | **FlowDiagram** — multi-tier architecture | `SVGComponent` + `FlowDiagram` |
| `create_buy_flow` | 162-261 | **FlowDiagram** — step-by-step flow | `SVGComponent` + `FlowDiagram` |
| `create_erd` | 263-346 | **ComparisonTable** — entity tables + relationship lines | `SVGComponent` + `ComparisonTable` + custom lines |
| `create_dashboard` | 348-481 | **Partial** — SVGComponent helpers only (dashboard mockup too UI-specific) | `SVGComponent.header/background/title/footer` + custom content |
| `create_progress` | 483-537 | **Partial** — SVGComponent helpers only (simple progress bars) | `SVGComponent` structure + custom bars |
| `create_flower` | 539-637 | **Partial** — SVGComponent helpers only (monitoring dashboard mockup) | `SVGComponent` structure + custom content |

Migrate in order: `create_architecture` → `create_buy_flow` → `create_erd`. Then partially migrate the remaining three.

**Step 4: For each method, follow the cycle:**
1. Read the full current method body
2. Identify nodes/edges for FlowDiagram or headers/rows for ComparisonTable
3. Rewrite method body using components
4. Run regression test for that specific method
5. Visual verify the SVG

**Step 5: Run full regression suite, lint, commit**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestKISTradingMigration -v --no-header
uv run ruff check blog/images/kis_trading_images.py && uv run ruff format --check blog/images/kis_trading_images.py
git add blog/images/kis_trading_images.py blog/tests/test_image_migration.py
git commit -m "refactor(blog): migrate kis_trading_images.py — FlowDiagram for architecture/buy flow, ComparisonTable for ERD, SVGComponent for dashboard/progress/flower"
```

---

## Task 5: Migrate `openclaw_images.py` — architecture, SSH tunnel, auth flow

**Files:**
- Modify: `blog/images/openclaw_images.py`
- Modify: `blog/tests/test_image_migration.py`

**Step 1: Write regression tests**

Add `TestOpenClawMigration` class. Key assertions: content includes "OpenClaw", "FastAPI", "SSH", "Callback", etc.

**Step 2: Run baseline**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestOpenClawMigration -v --no-header
```

**Step 3: Migrate methods**

This is the biggest payoff: `openclaw_images.py` uses 122 calls to `self.rect/text/line/circle` helper methods. All three diagram methods are architecture/flow patterns → `FlowDiagram`.

| Method | Lines | Strategy |
|--------|-------|----------|
| `create_thumbnail` | 23-38 | **SKIP** — already uses ThumbnailTemplate |
| `create_architecture` | 40-384 | **FlowDiagram** — async pipeline architecture (344 lines → ~80) |
| `create_ssh_tunnel` | 386-673 | **FlowDiagram** — network layer diagram (287 lines → ~70) |
| `create_auth_flow` | 676-890 | **FlowDiagram** — callback auth flow (214 lines → ~60) |

For each method:
1. Read the full method body carefully
2. Extract the logical nodes (boxes with labels and colors)
3. Extract the logical edges (arrows with labels)
4. Also extract any layer headers, background sections, annotations that need custom SVG
5. Rewrite as `SVGComponent.header/background/title` + layer backgrounds (custom) + `FlowDiagram.create(nodes, edges)` + annotations (custom) + `SVGComponent.footer()`

**Important:** `openclaw_images.py` doesn't use `sys.path.insert` at the top — it imports directly. After migration, add the component import:

```python
from blog.tools.components.base import SVGComponent
from blog.tools.components.flow_diagram import FlowDiagram
```

**Step 4: Run regression tests, visual verify, lint, commit**

```bash
uv run python -m pytest blog/tests/test_image_migration.py::TestOpenClawMigration -v --no-header
uv run ruff check blog/images/openclaw_images.py && uv run ruff format --check blog/images/openclaw_images.py
git add blog/images/openclaw_images.py blog/tests/test_image_migration.py
git commit -m "refactor(blog): migrate openclaw_images.py — FlowDiagram replaces 122 helper calls across 3 diagram methods"
```

---

## Task 6: Deprecate unused `BlogImageGenerator` helper methods

**Files:**
- Modify: `blog/tools/image_generator.py`

**Step 1: Check if any script still uses the old helpers**

```bash
# Search for self.rect, self.text, self.line, self.circle, self.svg_header, self.svg_footer, self.gradient_defs
uv run python -c "
import ast, sys
sys.path.insert(0, '.')
helpers = {'rect', 'text', 'line', 'circle', 'svg_header', 'svg_footer', 'gradient_defs'}
for script in ['blog/images/kis_trading_images.py', 'blog/images/openclaw_images.py',
               'blog/images/mcp_server_images.py', 'blog/images/python314_images.py']:
    with open(script) as f:
        source = f.read()
    used = [h for h in helpers if f'self.{h}' in source]
    print(f'{script}: {used or \"none\"}')"
```

**Step 2: If no script uses the old helpers, add deprecation docstrings**

Do NOT delete the methods yet — they're part of the public API. Add deprecation notice:

```python
    @staticmethod
    def svg_header(width: int, height: int, defs: str = "") -> str:
        """SVG 헤더 생성.

        .. deprecated::
            Use ``SVGComponent.header()`` from ``blog.tools.components.base`` instead.
        """
```

**Step 3: Commit**

```bash
git add blog/tools/image_generator.py
git commit -m "refactor(blog): deprecate BlogImageGenerator SVG helpers — prefer SVGComponent"
```

---

## Task 7: Final verification — all scripts generate successfully

**Files:** None modified — read-only verification.

**Step 1: Run full blog test suite**

```bash
uv run python -m pytest blog/tests/ -v --no-header
```

**Step 2: Generate all scripts end-to-end (SVG only, skip PNG for speed)**

```bash
uv run python -c "
import sys; sys.path.insert(0, '.')
from blog.images.python314_images import Python314Images
from blog.images.mcp_server_images import MCPServerImages
from blog.images.kis_trading_images import KISTradingImages
from blog.images.openclaw_images import OpenClawImages
from blog.images.samsung_analysis_images import SamsungAnalysisImages

for cls, prefix in [
    (Python314Images, 'python314_upgrade'),
    (MCPServerImages, 'mcp_server'),
    (KISTradingImages, 'kis_trading'),
    (OpenClawImages, 'openclaw'),
    (SamsungAnalysisImages, None),
]:
    gen = cls() if prefix is None else cls(prefix)
    paths = gen.generate_svgs()
    total_kb = sum(p.stat().st_size for p in paths) / 1024
    print(f'✅ {type(gen).__name__}: {len(paths)} SVGs ({total_kb:.1f} KB)')
"
```

Expected: 5 generators × N SVGs = ~20 total SVG files, all generated without errors.

**Step 3: Report line count reduction**

```bash
wc -l blog/images/*_images.py
```

Compare with pre-migration baseline (2042 total lines).

**Step 4: Commit any fixups**

```bash
git add -A && git commit -m "fix(blog): migration fixups from end-to-end verification" || echo "Nothing to commit — all clean"
```

---

## Summary

| Task | What | Depends On |
|------|------|-----------|
| 1 | Create `samsung_analysis_images.py` (StockAnalysisPreset wrapper) | Phase 1 |
| 2 | Migrate `python314_images.py` (2 methods) | Task 1 test file |
| 3 | Migrate `mcp_server_images.py` (2 methods) | Task 2 test file |
| 4 | Migrate `kis_trading_images.py` (6 methods, 3 full + 3 partial) | Task 3 test file |
| 5 | Migrate `openclaw_images.py` (3 methods, biggest reduction) | Task 4 test file |
| 6 | Deprecate old `BlogImageGenerator` helpers | Tasks 2–5 |
| 7 | Final end-to-end verification | Task 6 |

**Parallelizable:** Tasks 2, 3, 4, 5 are independent (each migrates a different script). Could run in parallel with separate agents.

**Not in scope:**
- Enhancing components (e.g., adding GanttChart, PairedBarChart) — follow-up task
- PNG conversion testing — visual verification is manual (SVG in browser)
- Removing `BlogImageGenerator` helper methods — just deprecation notice for now
