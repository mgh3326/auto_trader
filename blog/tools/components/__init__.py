"""Reusable SVG building blocks.

All components return SVG fragment strings via static create() methods.
Compose fragments with SVGComponent.header() / .footer() for complete documents.
"""

from blog.tools.components.bar_chart import BarChart
from blog.tools.components.base import (
    Colors,
    SVGComponent,
    escape_xml,
    format_large,
    format_pct,
    format_price,
)
from blog.tools.components.card import InfoCard
from blog.tools.components.code_block import CodeBlock
from blog.tools.components.flow_diagram import FlowDiagram
from blog.tools.components.table import ComparisonTable
from blog.tools.components.thumbnail import ThumbnailTemplate
from blog.tools.components.timeline import EventTimeline

__all__ = [
    "BarChart",
    "CodeBlock",
    "Colors",
    "ComparisonTable",
    "EventTimeline",
    "FlowDiagram",
    "InfoCard",
    "SVGComponent",
    "ThumbnailTemplate",
    "escape_xml",
    "format_large",
    "format_pct",
    "format_price",
]
