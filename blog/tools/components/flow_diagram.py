"""FlowDiagram SVG component.

Nodes connected by labeled arrows for architecture diagrams.
"""

from __future__ import annotations

from blog.tools.components.base import escape_xml


class FlowDiagram:
    """Flow diagram — nodes with labeled connecting edges."""

    @staticmethod
    def create(
        nodes: list[tuple[int, int, int, int, str, str]],  # (x, y, w, h, label, color)
        edges: list[tuple[int, int, str]],  # (from_idx, to_idx, label)
    ) -> str:
        """Render a flow diagram as an SVG fragment.

        Args:
            nodes: List of (x, y, width, height, label, color) tuples.
            edges: List of (from_node_idx, to_node_idx, label) tuples.

        Returns:
            SVG fragment string (no <svg> wrapper).
        """
        parts: list[str] = []

        # Draw edges first (behind nodes)
        for from_idx, to_idx, label in edges:
            if from_idx < len(nodes) and to_idx < len(nodes):
                fx, fy, fw, fh, _, _ = nodes[from_idx]
                tx, ty, tw, th, _, _ = nodes[to_idx]

                # Calculate connection points (center to center)
                x1 = fx + fw // 2
                y1 = fy + fh // 2
                x2 = tx + tw // 2
                y2 = ty + th // 2

                # Adjust to edge of nodes
                dx = x2 - x1
                dy = y2 - y1
                dist = (dx ** 2 + dy ** 2) ** 0.5
                if dist > 0:
                    # Offset by half node size (approximate)
                    offset1 = min(fw, fh) // 2
                    offset2 = min(tw, th) // 2
                    x1 += dx / dist * offset1
                    y1 += dy / dist * offset1
                    x2 -= dx / dist * offset2
                    y2 -= dy / dist * offset2

                parts.append(
                    f'    <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                    f'stroke="#666666" stroke-width="2" marker-end="url(#arrowhead)"/>'
                )

                # Edge label at midpoint
                if label:
                    mx = (x1 + x2) // 2
                    my = (y1 + y2) // 2
                    safe_label = escape_xml(label)
                    parts.append(
                        f'    <text x="{mx}" y="{my - 5}" '
                        f'font-family="Arial, sans-serif" font-size="11" '
                        f'fill="#666666" text-anchor="middle" '
                        f'fill="#ffffff" stroke="#ffffff" stroke-width="3" '
                        f'paint-order="stroke">{safe_label}</text>'
                    )
                    parts.append(
                        f'    <text x="{mx}" y="{my - 5}" '
                        f'font-family="Arial, sans-serif" font-size="11" '
                        f'fill="#666666" text-anchor="middle">{safe_label}</text>'
                    )

        # Draw nodes
        for x, y, w, h, label, color in nodes:
            # Node rectangle
            parts.append(
                f'    <rect x="{x}" y="{y}" width="{w}" height="{h}" '
                f'fill="{color}" rx="8" stroke="#333333" stroke-width="1"/>'
            )

            # Node label
            safe_label = escape_xml(label)
            parts.append(
                f'    <text x="{x + w // 2}" y="{y + h // 2 + 5}" '
                f'font-family="Arial, sans-serif" font-size="14" '
                f'font-weight="bold" fill="#ffffff" text-anchor="middle">'
                f'{safe_label}</text>'
            )

        return "\n".join(parts) + "\n"
