"""ROB-273 — snapshot-backed advisory report generation.

Public surface:

* :class:`~app.services.action_report.snapshot_backed.generator.SnapshotBackedReportGenerator`
* :class:`~app.services.action_report.snapshot_backed.request.ReportGenerationRequest`
* :class:`~app.services.action_report.snapshot_backed.request.ReportGenerationResponse`
* :func:`~app.services.action_report.snapshot_backed.collectors.registry.production_collector_registry`

This package owns the wiring between the bundle-ensure service and the
investment-reports ingestion service. Collectors live under
``collectors/``; they are all read-only by contract.
"""
