"""Mechanical loss-cut acting on per-lot exit plans (ROB-1460).

A "lot" here is one account_mode + market + symbol position. Its exit plan is
recorded when a buy is accepted and gets a new version on every add.

Import nothing heavy here: ``gate`` and ``rules`` are read from the order
proposal classifier.
"""
