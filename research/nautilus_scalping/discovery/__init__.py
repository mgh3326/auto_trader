"""ROB-339 — pure-parquet scalping strategy discovery / fast-screen package.

Imports nothing from ``app`` and nothing from the Nautilus engine. Produces only
non-canonical recommendations (screened_out / needs_more_data /
promote_to_full_validation); the conservative gate owns ``validated``.
"""
