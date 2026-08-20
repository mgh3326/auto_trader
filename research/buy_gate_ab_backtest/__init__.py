"""ROB-1301 historical A/B buy-gate backtest (research-only, offline).

This package re-applies the *pre-registered* ROB-1301 gate and scoring code
(``app.services.buy_gate_ab_shadow``) to frozen historical corpora. It never
touches a broker, the operating DB, or the network, and it never proposes,
orders, or watches anything.

It does not replace the live 4-week shadow collection. It is a prior-sample
answer to the same question, and it carries proxy limits the live run does not.
"""
