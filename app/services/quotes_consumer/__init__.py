"""Records-only ``quotes:toss`` stream consumer (#1120, Q-109 = A shadow).

Reads the fillwire quote stream, evaluates the four R3 spike triggers once
per second as pure shadow firings, and records ladder rung approach /
touch / fill events.  The package holds no order, session-kick, LLM,
broker, or scheduling path — see
``docs/design/1120-quotes-toss-shadow-consumer.md``.
"""
