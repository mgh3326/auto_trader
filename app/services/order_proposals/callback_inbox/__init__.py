"""W5 — durable Telegram callback inbox.

PostgreSQL is the authority for an order-adjacent approval click; TaskIQ is a
best-effort wake-up carrying nothing but an opaque job UUID. Losing Redis
loses latency, never a click.

Deliberately re-exports nothing. ``app.models.telegram_callback_inbox`` derives
its CHECK constraints from :mod:`.contracts`, and importing that submodule runs
this ``__init__`` first -- so any eager import here of a module that touches the
ORM closes an import cycle. Import the submodule you want:

* :mod:`.contracts` -- closed vocabularies, digests, the advisory-lock key
* :mod:`.ingress`   -- ``ingest_callback_update``
* :mod:`.worker`    -- ``process_callback_job``
* :mod:`.recovery`  -- ``recover_callback_jobs``

Runbook: ``docs/runbooks/telegram-callback-durable-inbox.md``.
"""
