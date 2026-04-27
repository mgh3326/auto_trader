"""Service package.

Keep package import side-effect free. Import concrete service modules directly at
call sites (for example, ``from app.services import order_service``) so safety
checks can reason about which execution paths are actually loaded.
"""
