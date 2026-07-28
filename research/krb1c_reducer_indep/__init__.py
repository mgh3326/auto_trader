"""KRB1C C_stress_cap deterministic reducer — INDEPENDENT REIMPLEMENTATION.

This package is a clean-room reimplementation of the sealed clause

    KRB1-CSM60-H5-v1 / amendment KRB1C-CSTRESS-REDUCER-v1, §6.2.1

written per §7.7 step 3 ("참조 구현을 import·복사하지 않는 독립 재현기").

Independence contract
---------------------
Written solely from the sealed normative text carried in

    krb1c-amendment-canonical-2026-07-28.json
    sha256 d5da5edd6b49fb759b781c13f627e21a84667ced2be7cf03624a40bb813be389

plus the sealed P0-1 KRX tick table and the sealed P0-2 cost binding.
The reference implementation was not read, imported, or copied.

Exactness contract
------------------
Every numeric quantity is ``int`` or ``fractions.Fraction``. No ``float``,
no ``decimal.Decimal``, no ``math`` module. Enforced by
``tests/test_no_float.py`` (AST scan) and by runtime type assertions.

Scope
-----
This package performs the §7.7 step-3/step-4 reproduction only. It does NOT
produce a P0-2 completion hash and is not the "정확히 1회" binding numeric
reducer execution of §7.2.
"""

from .tick import (
    KOSDAQ_TICK_TABLE,
    KOSPI_TICK_TABLE,
    TickTable,
    TickBand,
)
from .reducer import (
    CandidateRow,
    MarketCostInput,
    MarketResult,
    ReducerResult,
    reduce_c_stress_cap,
)

__all__ = [
    "TickBand",
    "TickTable",
    "KOSPI_TICK_TABLE",
    "KOSDAQ_TICK_TABLE",
    "MarketCostInput",
    "CandidateRow",
    "MarketResult",
    "ReducerResult",
    "reduce_c_stress_cap",
]
