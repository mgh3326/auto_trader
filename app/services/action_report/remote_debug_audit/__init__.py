"""ROB-323 follow-up — operator-run Naver remote-debug data-quality audit.

This package is operator-tooling only. It is NEVER imported by the
report-generation hot path (``snapshot_backed.generator`` /
``collectors.registry``) — the registry stubs stay fail-open. See
``test_no_hotpath_import.py``.
"""
