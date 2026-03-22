"""CodeBlock SVG component.

Dark-themed code snippet display with optional language label.
"""

from __future__ import annotations

from blog.tools.components.base import FONT_FAMILY, escape_xml


class CodeBlock:
    """Code block — dark background with monospace text."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        code: str,
        language: str = "",
        line_numbers: bool = False,
    ) -> str:
        """Render a code block as an SVG fragment.

        Args:
            x: X position of the code block.
            y: Y position of the code block.
            width: Block width.
            height: Block height.
            code: Code text (may contain newlines).
            language: Optional language label (shown top-right).
            line_numbers: Whether to show line numbers.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # Dark background
        parts.append(
            f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'fill="#1e1e1e" rx="6" stroke="#333333" stroke-width="1"/>'
        )

        # Language label
        if language:
            safe_lang = escape_xml(language)
            parts.append(
                f'    <text x="{x + width - 10}" y="{y + 20}" '
                f'font-family="{FONT_FAMILY}" font-size="11" '
                f'fill="#666666" text-anchor="end">{safe_lang}</text>'
            )

        # Code lines
        lines = code.split("\n")
        line_height = 18
        start_y = y + 30
        left_margin = x + (40 if line_numbers else 15)

        for i, line in enumerate(lines):
            line_y = start_y + i * line_height

            if line_y + line_height > y + height:
                break  # Don't overflow

            # Line number
            if line_numbers:
                parts.append(
                    f'    <text x="{x + 10}" y="{line_y}" '
                    f'font-family="monospace" font-size="12" '
                    f'fill="#666666">{i + 1:3d}</text>'
                )

            # Code text
            safe_line = escape_xml(line.replace("\t", "    "))  # Tabs to spaces
            if safe_line.strip():  # Only add non-empty lines
                parts.append(
                    f'    <text x="{left_margin}" y="{line_y}" '
                    f'font-family="monospace" font-size="12" '
                    f'fill="#d4d4d4">{safe_line}</text>'
                )

        return "\n".join(parts) + "\n"
