"""Read-only retrospective readers for the retro/v1 provider contract.

One module per operation of ``contracts/retro/v1/openapi.yaml`` (except
``retro_scoreboard``, which is a separate reader). Each ``read_<operation>``
takes an idle ``AsyncSession`` plus the contract's query parameters and returns
the exact response body. The retro/v1 HTTP route calls them; no MCP tool or
scheduler is connected.
"""

from app.services.retro_v1.data_gaps import read_data_gaps
from app.services.retro_v1.envelope import RetroReadError
from app.services.retro_v1.get_bars import read_get_bars
from app.services.retro_v1.mock_experiments import read_mock_experiments
from app.services.retro_v1.retro_day import read_retro_day
from app.services.retro_v1.retro_decisions import read_retro_decisions
from app.services.retro_v1.retro_funnel import read_retro_funnel
from app.services.retro_v1.retro_positions import read_retro_positions
from app.services.retro_v1.retro_symbol import read_retro_symbol
from app.services.retro_v1.retro_trades import read_retro_trades

__all__ = [
    "RetroReadError",
    "read_data_gaps",
    "read_get_bars",
    "read_mock_experiments",
    "read_retro_day",
    "read_retro_decisions",
    "read_retro_funnel",
    "read_retro_positions",
    "read_retro_symbol",
    "read_retro_trades",
]
