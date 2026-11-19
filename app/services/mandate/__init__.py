"""Mandate model (hk 3795): clauses, approvals, activation profiles and split plans.

T1 holds the data model, the DB guards' Python twins, the pure invariants and the
only writer. Nothing here sends an order, reads a broker or opens an approval
gate; later tickets (T2 onward) wire it up. Keep this package free of broker and
order-path imports (``tests/services/mandate/test_static_boundary.py``).
"""
