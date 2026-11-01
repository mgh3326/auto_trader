"""U7-KR boosting baseline + frozen controls (ROB-1441). Research only.

No broker, account, database, scheduler or ``app`` import. LightGBM is imported
lazily by ``train`` only and lives in ``requirements-ml.txt``, outside the
production package; every other module runs on the repository's test harness.
"""
