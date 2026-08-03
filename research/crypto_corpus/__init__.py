"""Public, venue-separated crypto OHLCV corpus builder.

This package is intentionally research-only: it has no application, database,
broker, account, scheduler, credential, or LLM dependencies.
"""

from .constants import CORPUS_ID
from .loader import (
    LabeledCorpus,
    inspect_parquet_policy,
    load_labeled_parquet,
    load_labeled_parquet_files,
)
from .policy import (
    CrossVenueReadForbidden,
    HoldoutReadForbidden,
    ParquetPolicyMismatchError,
    UnlabeledParquetError,
    UpbitXsecOptInRequired,
    VenuePolicy,
)

__all__ = [
    "CORPUS_ID",
    "CrossVenueReadForbidden",
    "HoldoutReadForbidden",
    "LabeledCorpus",
    "ParquetPolicyMismatchError",
    "UnlabeledParquetError",
    "UpbitXsecOptInRequired",
    "VenuePolicy",
    "inspect_parquet_policy",
    "load_labeled_parquet",
    "load_labeled_parquet_files",
]
