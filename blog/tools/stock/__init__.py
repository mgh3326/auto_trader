"""Stock analysis specific SVG components.

These components consume MCP API response dicts and produce
SVG fragments for stock analysis images.
"""

from blog.tools.stock.candlestick_chart import CandlestickChart
from blog.tools.stock.conclusion_card import ConclusionCard
from blog.tools.stock.earnings_chart import EarningsChart
from blog.tools.stock.indicator_dashboard import IndicatorDashboard
from blog.tools.stock.investor_flow import InvestorFlow
from blog.tools.stock.opinion_table import OpinionTable
from blog.tools.stock.price_chart import PriceChart
from blog.tools.stock.support_resistance import SupportResistance
from blog.tools.stock.valuation_cards import ValuationCards

__all__ = [
    "CandlestickChart",
    "ConclusionCard",
    "EarningsChart",
    "IndicatorDashboard",
    "InvestorFlow",
    "OpinionTable",
    "PriceChart",
    "SupportResistance",
    "ValuationCards",
]
