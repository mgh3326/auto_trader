"""BarChart SVG component.

Horizontal or vertical bar charts for comparing values.
Supports positive/negative values with automatic scaling.
"""

from __future__ import annotations

from blog.tools.components.base import escape_xml


class BarChart:
    """Bar chart component — horizontal or vertical bars."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        data: list[tuple[str, float, str]],
        direction: str = "vertical",
        show_labels: bool = True,
        chart_title: str = "",
        bar_padding: float = 0.2,
    ) -> str:
        """Render a bar chart as an SVG fragment.

        Args:
            x: X position of the chart.
            y: Y position of the chart.
            width: Chart width.
            height: Chart height.
            data: List of (label, value, color) tuples.
            direction: "vertical" or "horizontal".
            show_labels: Whether to show axis labels.
            chart_title: Optional title above the chart.
            bar_padding: Padding between bars (0-1, fraction of bar width).

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        if not data:
            return f"    <!-- Empty bar chart at ({x}, {y}) -->\n"

        parts: list[str] = []

        # Title
        title_offset = 0
        if chart_title:
            title_offset = 30
            safe_title = escape_xml(chart_title)
            parts.append(
                f'    <text x="{x + width // 2}" y="{y + 20}" '
                f'font-family="Arial, sans-serif" font-size="16" '
                f'font-weight="bold" fill="#333333" text-anchor="middle">'
                f"{safe_title}</text>"
            )

        # Calculate chart area (after title)
        chart_y = y + title_offset
        chart_height = height - title_offset

        if direction == "vertical":
            # Vertical bars
            max_val = max(abs(v) for _, v, _ in data) if data else 1
            if max_val == 0:
                max_val = 1

            bar_width = width / len(data) * (1 - bar_padding)
            bar_gap = width / len(data) * bar_padding

            for i, (label, value, color) in enumerate(data):
                bar_x = x + i * (bar_width + bar_gap) + bar_gap / 2
                bar_h = (
                    abs(value) / max_val * (chart_height - 40)
                )  # Leave room for labels
                bar_y = (
                    chart_y + chart_height - 40 - bar_h
                    if value >= 0
                    else chart_y + chart_height - 40
                )

                parts.append(
                    f'    <rect x="{bar_x}" y="{bar_y}" width="{bar_width}" '
                    f'height="{bar_h}" fill="{color}" rx="2"/>'
                )

                if show_labels:
                    safe_label = escape_xml(str(label))
                    parts.append(
                        f'    <text x="{bar_x + bar_width / 2}" '
                        f'y="{chart_y + chart_height - 20}" '
                        f'font-family="Arial, sans-serif" font-size="12" '
                        f'fill="#666666" text-anchor="middle">{safe_label}</text>'
                    )
        else:
            # Horizontal bars
            values = [v for _, v, _ in data]
            max_abs = max(abs(v) for v in values) if values else 1
            if max_abs == 0:
                max_abs = 1

            # Center line for zero
            zero_x = (
                x + width * (max_abs / (2 * max_abs))
                if any(v < 0 for v in values)
                else x
            )

            bar_height = chart_height / len(data) * (1 - bar_padding)
            bar_gap = chart_height / len(data) * bar_padding

            for i, (label, value, color) in enumerate(data):
                bar_y = chart_y + i * (bar_height + bar_gap) + bar_gap / 2
                bar_w = abs(value) / max_abs * (width - 100)  # Leave room for labels

                if value >= 0:
                    bar_x = zero_x
                else:
                    bar_x = zero_x - bar_w

                parts.append(
                    f'    <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" '
                    f'height="{bar_height}" fill="{color}" rx="2"/>'
                )

                if show_labels:
                    safe_label = escape_xml(str(label))
                    parts.append(
                        f'    <text x="{x}" y="{bar_y + bar_height / 2 + 4}" '
                        f'font-family="Arial, sans-serif" font-size="12" '
                        f'fill="#666666" text-anchor="start">{safe_label}</text>'
                    )

        return "\n".join(parts) + "\n"
