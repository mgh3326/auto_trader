"""Self-check for the us-corpus-v1 safety gates. No network, no files written.

`research/` is outside `testpaths`, so this is not collected by CI. It exists so
a reviewer can re-run the boundary proofs by hand:

    uv run python -m research.us_corpus.verify_gates

Each check asserts a gate *refuses* something it must refuse. A gate that only
works when nothing is wrong is not a gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.symbol import to_yahoo_symbol  # noqa: E402
from research.us_corpus import config as cfg  # noqa: E402
from research.us_corpus.alpaca_probe import (  # noqa: E402
    HostBoundaryViolation,
    assert_data_host,
)
from research.us_corpus.build import project_budget  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(name)


def refuses(url: str) -> bool:
    try:
        assert_data_host(url)
    except HostBoundaryViolation:
        return True
    return False


def main() -> int:
    print("== Alpaca host boundary (must REFUSE) ==")
    check("live trading host", refuses("https://api.alpaca.markets/v2/account"))
    check("paper trading host", refuses("https://paper-api.alpaca.markets/v2/orders"))
    check("broker host", refuses("https://broker-api.alpaca.markets/v1/accounts"))
    check(
        "account path on data host", refuses("https://data.alpaca.markets/v2/account")
    )
    check(
        "positions path on data host",
        refuses("https://data.alpaca.markets/v2/positions"),
    )
    check("plaintext http", refuses("http://data.alpaca.markets/v2/stocks/AAPL/bars"))
    check("foreign host", refuses("https://evil.example/v2/stocks/AAPL/bars"))

    print("\n== Alpaca host boundary (must ALLOW) ==")
    allowed = not refuses("https://data.alpaca.markets/v2/stocks/AAPL/bars")
    check("market-data bars path", allowed)

    print("\n== Request budget gate ==")
    projected, _ = project_budget(cfg.UNIVERSE_COUNT)
    check(
        "full universe fits the cap",
        projected <= cfg.MAX_REQUESTS,
        f"{projected} <= {cfg.MAX_REQUESTS}",
    )
    over, _ = project_budget(cfg.MAX_REQUESTS * 2)
    check(
        "an oversized universe would block",
        over > cfg.MAX_REQUESTS,
        f"{over} > {cfg.MAX_REQUESTS}",
    )

    print("\n== Symbol mapping (shared app.core.symbol, not reimplemented) ==")
    check("BRK.B -> BRK-B", to_yahoo_symbol("BRK.B") == "BRK-B")
    check("AKO.A -> AKO-A", to_yahoo_symbol("AKO.A") == "AKO-A")
    check("plain symbol unchanged", to_yahoo_symbol("AAPL") == "AAPL")

    print("\n== Pinned input digests ==")
    try:
        cfg.verify_inputs()
        check("both pinned inputs match their SHA-256", True)
    except cfg.PreconditionFailed as exc:
        check("both pinned inputs match their SHA-256", False, str(exc))

    print("\n== Config invariants ==")
    check("survivorship label is set", cfg.SURVIVORSHIP_BIASED is True)
    check("no fallback source configured", cfg.SOURCE_FALLBACK is None)
    check("operating db writes budgeted at zero", cfg.OPERATING_DB_WRITES == 0)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
