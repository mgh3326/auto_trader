# Pytest PostgreSQL ownership

Database-backed pytest selections create one database per serial run or xdist
worker. Names have the exact shape
`test_db_pytest_<12 lowercase hex>_<main|gwN>`. The database also contains
`public._pytest_database_owner`; teardown verifies its random ownership token
before issuing an exact-name `DROP DATABASE`.

Pure unit and collect-only selections choose a name but do not create or connect
to PostgreSQL. The first selected integration/DB fixture owns creation, schema
bootstrap, and cleanup.

The legacy shared `test_db` behavior is available only by explicit opt-in:

```bash
AUTO_TRADER_PYTEST_USE_SHARED_DB=1 uv run pytest ...
```

`AUTO_TRADER_TEST_DATABASE_URL` may change connection details, but it must still
target a PostgreSQL database named exactly `test_db`. A different/general
database is rejected before pytest can use it. When this override is used with
shared mode, its host, port, and username become the explicitly selected test
server identity; setting it to a production server is an operator error and
is not a safety bypass. Shared mode never drops `test_db`.

## Interrupted-run cleanup

Normal `SIGINT` lets pytest finalizers remove the owned database. `SIGKILL`,
host crashes, or PostgreSQL loss can leave an exact run-owned database behind.
List candidates without deleting anything:

```sql
SELECT datname
FROM pg_database
WHERE datname LIKE 'test_db_pytest_%'
ORDER BY datname;
```

For each exact candidate, connect to that one database and inspect:

```sql
SELECT database_name, owner_token, created_at
FROM public._pytest_database_owner;
```

Only after the exact name and marker have been reviewed should an operator run
`DROP DATABASE "<exact database name>" WITH (FORCE)` from another database.
Never automate cleanup with a wildcard or drop a database lacking the ownership
marker.
