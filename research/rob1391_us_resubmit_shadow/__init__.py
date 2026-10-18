"""ROB-1391 -- US original-limit 3-session resubmission SHADOW experiment.

Research only. No order, proposal, watch or ledger write path; no broker
client; the production database is read inside a read-only transaction and the
only network host is the public market-data host. See README.md.
"""
