"""Isolated, resumable builder for the ``kr-corpus-v1`` research corpus.

This package deliberately has no imports from ``app`` and no runtime trading,
database, scheduler, or LLM dependencies.  Its only network integration is a
lazy pykrx adapter used by the explicit command-line entry point.
"""

from __future__ import annotations
