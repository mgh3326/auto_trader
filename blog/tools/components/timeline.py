"""EventTimeline SVG component.

Horizontal timeline with event markers positioned above/below the axis.
"""

from __future__ import annotations

from blog.tools.components.base import escape_xml


class EventTimeline:
    """Event timeline — horizontal axis with labeled markers."""

    @staticmethod
    def create(
        x: int,
        y: int,
        width: int,
        height: int,
        events: list[
            tuple[str, str, str, str, str]
        ],  # (date, label, value, color, position)
        axis_color: str = "#666666",
    ) -> str:
        """Render an event timeline as an SVG fragment.

        Args:
            x: X position of the timeline.
            y: Y position of the timeline.
            width: Timeline width.
            height: Timeline height.
            events: List of (date, label, value, color, position) tuples.
                    position is "above" or "below" the axis.
            axis_color: Color of the timeline axis.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        axis_y = y + height // 2

        # Main axis line
        parts.append(
            f'    <line x1="{x}" y1="{axis_y}" x2="{x + width}" '
            f'y2="{axis_y}" stroke="{axis_color}" stroke-width="2"/>'
        )

        if not events:
            return "\n".join(parts) + "\n"

        # Position events evenly
        spacing = width / (len(events) + 1)

        for i, (date, label, value, color, position) in enumerate(events):
            event_x = x + (i + 1) * spacing

            # Marker circle
            parts.append(
                f'    <circle cx="{event_x}" cy="{axis_y}" r="6" '
                f'fill="{color}" stroke="#ffffff" stroke-width="2"/>'
            )

            # Vertical connector line
            connector_length = 30
            if position == "above":
                connector_y1 = axis_y - 6
                connector_y2 = axis_y - connector_length
                label_y = connector_y2 - 10
                date_y = label_y - 18
                value_y = label_y + 5
            else:
                connector_y1 = axis_y + 6
                connector_y2 = axis_y + connector_length
                date_y = connector_y2 + 18
                label_y = date_y + 5
                value_y = date_y + 23

            parts.append(
                f'    <line x1="{event_x}" y1="{connector_y1}" '
                f'x2="{event_x}" y2="{connector_y2}" '
                f'stroke="{color}" stroke-width="1" stroke-dasharray="3,3"/>'
            )

            # Date label
            safe_date = escape_xml(date)
            parts.append(
                f'    <text x="{event_x}" y="{date_y}" '
                f'font-family="Arial, sans-serif" font-size="11" '
                f'font-weight="bold" fill="{color}" text-anchor="middle">'
                f"{safe_date}</text>"
            )

            # Event label
            safe_label = escape_xml(label)
            parts.append(
                f'    <text x="{event_x}" y="{label_y}" '
                f'font-family="Arial, sans-serif" font-size="10" '
                f'fill="#333333" text-anchor="middle">'
                f"{safe_label}</text>"
            )

            # Value label
            safe_value = escape_xml(value)
            parts.append(
                f'    <text x="{event_x}" y="{value_y}" '
                f'font-family="Arial, sans-serif" font-size="10" '
                f'fill="#666666" text-anchor="middle">'
                f"{safe_value}</text>"
            )

        return "\n".join(parts) + "\n"
