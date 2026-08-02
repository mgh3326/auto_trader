"""ROB-1036 — ``uber-invalid-sample-eligibility.v1``.

Import the submodules directly. This package intentionally re-exports nothing:
``read_model`` imports ``trade_journal.forecast_service``, which imports
``contract``, so an eager re-export here would close an import cycle.
"""
