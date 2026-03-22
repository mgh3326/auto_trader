"""ComparisonTable SVG component.

Header/row table with optional row highlighting.
"""

from __future__ import annotations

from blog.tools.components.base import FONT_FAMILY, escape_xml


class ComparisonTable:
    """Comparison table — header row + data rows with grid lines."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        headers: list[str],
        rows: list[list[str]],
        highlight_row: int | None = None,
        table_title: str = "",
        header_color: str = "#2196F3",
    ) -> str:
        """Render a comparison table as an SVG fragment.

        Args:
            x: X position of the table.
            y: Y position of the table.
            width: Table width.
            height: Table height.
            headers: List of header column labels.
            rows: List of row data (each row is a list of strings).
            highlight_row: Optional index of row to highlight.
            table_title: Optional title above the table.
            header_color: Background color for header row.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # Title
        title_offset = 0
        if table_title:
            title_offset = 30
            safe_title = escape_xml(table_title)
            parts.append(
                f'    <text x="{x + width // 2}" y="{y + 20}" '
                f'font-family="{FONT_FAMILY}" font-size="16" '
                f'font-weight="bold" fill="#333333" text-anchor="middle">'
                f"{safe_title}</text>"
            )

        table_y = y + title_offset
        table_height = height - title_offset

        # Calculate row height
        total_rows = 1 + len(rows)  # header + data rows
        row_height = table_height / total_rows

        # Calculate column widths
        col_width = width / len(headers) if headers else width

        # Header background
        parts.append(
            f'    <rect x="{x}" y="{table_y}" width="{width}" '
            f'height="{row_height}" fill="{header_color}" rx="4"/>'
        )

        # Header cells
        for i, header in enumerate(headers):
            safe_header = escape_xml(header)
            parts.append(
                f'    <text x="{x + i * col_width + col_width / 2}" '
                f'y="{table_y + row_height / 2 + 5}" '
                f'font-family="{FONT_FAMILY}" font-size="14" '
                f'font-weight="bold" fill="#ffffff" text-anchor="middle">'
                f"{safe_header}</text>"
            )

        # Data rows
        for row_idx, row in enumerate(rows):
            row_y = table_y + (row_idx + 1) * row_height

            # Highlight background if specified
            if highlight_row == row_idx:
                parts.append(
                    f'    <rect x="{x}" y="{row_y}" width="{width}" '
                    f'height="{row_height}" fill="#e3f2fd" rx="4"/>'
                )

            # Row separator line
            parts.append(
                f'    <line x1="{x}" y1="{row_y}" x2="{x + width}" '
                f'y2="{row_y}" stroke="#dee2e6" stroke-width="1"/>'
            )

            # Row cells
            for col_idx, cell in enumerate(row):
                if col_idx < len(headers):
                    safe_cell = escape_xml(str(cell))
                    parts.append(
                        f'    <text x="{x + col_idx * col_width + col_width / 2}" '
                        f'y="{row_y + row_height / 2 + 5}" '
                        f'font-family="{FONT_FAMILY}" font-size="12" '
                        f'fill="#333333" text-anchor="middle">'
                        f"{safe_cell}</text>"
                    )

        return "\n".join(parts) + "\n"
