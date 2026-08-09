"""``DFC_V22_RESEARCH_MIN`` (A2) — contract, schema and fail-closed validators.

This package is documentation-and-schema only.  It never fetches, lists or
downloads anything: the A2 *measurement* job is a separate relay unit, and this
package exists so that measurement can be judged against literals that were
frozen **before** any data was touched.

Modules:

``nw_verbatim``
    Byte-exact upstream clause text (NW-F2/F4/F5/F6).  Generated, never edited.
``contract``
    Frozen literals and clause IDs derived from that text.
``schema``
    Canonical parquet schemas and manifest key sets.
``validation``
    Fail-closed validators that raise :class:`~.validation.ContractViolation`.
"""

from __future__ import annotations
