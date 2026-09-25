"""NHPLUG (NH namuh) mock broker boundary.

Stage 1: account discovery, balance, and quote reads.  Stage 2 (#711): the
broker-verified ``acct_type=03`` mock account's KRX limit orders, modify, and
cancel through ``client.NHPlugMockClient`` only, plus the order listing read
interpreted by ``order_evidence``.  No live account, market order, scheduler,
or generic endpoint dispatch exists here.  ``live_quotes`` is a separate,
identity-free live period-quote client.
"""
