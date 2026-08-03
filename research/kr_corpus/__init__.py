"""KR corpus research surface for building and consuming historical corpora.

The collector surface is an isolated, resumable builder for the
``kr-corpus-v1`` research corpus.  The backtest harness and future corpus
consumers are separate research-facing surfaces in the same package.

This package deliberately has no imports from ``app`` and no runtime trading,
database, scheduler, or LLM dependencies.  Its only network integration is a
lazy pykrx adapter used by the explicit command-line entry point.
"""

from __future__ import annotations
