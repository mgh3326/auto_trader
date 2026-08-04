TABLES = {"marker": ("public", "_kr_dryrun_scratch_marker")}
SCHEMA, TABLE = TABLES["marker"]
SQL = "DROP TABLE %s.%s" % (SCHEMA, TABLE)  # noqa: UP031 - regression form
