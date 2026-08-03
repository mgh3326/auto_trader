"""Self-check for the us-corpus-v1 safety gates. No network, no files written.

`research/` is outside `testpaths`, so this is not collected by CI. It exists so
a reviewer can re-run the boundary proofs by hand:

    uv run python -m research.us_corpus.verify_gates

Each check asserts a gate *refuses* something it must refuse. A gate that only
works when nothing is wrong is not a gate.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.symbol import to_yahoo_symbol  # noqa: E402
from research.us_corpus import config as cfg  # noqa: E402
from research.us_corpus import holdout_gate  # noqa: E402
from research.us_corpus.alpaca_probe import (  # noqa: E402
    HostBoundaryViolation,
    assert_data_host,
)
from research.us_corpus.build import project_budget  # noqa: E402
from research.us_corpus.labeling import (  # noqa: E402
    UnlabeledCorpusError,
    read_labeled_parquet,
    verify_label,
    write_labeled_parquet,
)

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
    check("crosscheck pinned to v2", cfg.CROSSCHECK_VERSION == "v2")
    check(
        "v1 crosscheck retained as provenance",
        cfg.CROSSCHECK_SUPERSEDED_FILE.exists(),
    )

    print("\n== Holdout gate: path detection (must REFUSE) ==")
    hd = cfg.HOLDOUT_DIR
    for label, candidate in (
        ("holdout root", hd),
        ("child file", hd / "market=us" / "year=2025" / "part-00000.parquet"),
        ("dotdot re-entry", hd / ".." / "holdout" / "x.parquet"),
        ("case variant", Path(str(hd).replace("holdout", "HoLdOuT"))),
    ):
        check(f"detected as holdout: {label}", holdout_gate.is_under_holdout(candidate))
    check(
        "exploration path is NOT holdout",
        not holdout_gate.is_under_holdout(cfg.DATASET_DIR / "market=us"),
    )

    # 🔴 The access log must have a working READ path. R1's log could only ever
    # record writes, which made "written_not_read" unfalsifiable. Prove here
    # that an attempted read both raises AND lands in the log — against a temp
    # log, so this proof never touches the real ledger.
    print("\n== Holdout gate: read refusal is real and is logged ==")
    with tempfile.TemporaryDirectory() as tmp:
        temp_log = Path(tmp) / "access.log"
        sealed = hd / "market=us" / "year=2025" / "part-00000.parquet"
        raised = False
        try:
            holdout_gate.guard_read(sealed, "gate self-test", log_path=temp_log)
        except holdout_gate.HoldoutReadRefused:
            raised = True
        check("attempted holdout read raises", raised)
        logged = temp_log.read_text(encoding="utf-8") if temp_log.exists() else ""
        check("the attempt is recorded as READ", "\tREAD\t" in logged)
        check("record names the refusal", "REFUSED" in logged)

    print("\n== Static: no unguarded holdout access, no artifact-root sweep ==")
    offenders = holdout_gate.assert_no_unguarded_holdout_access()
    check(
        "no module outside the gate reaches holdout",
        not offenders,
        "; ".join(offenders) if offenders else "",
    )

    # 🔴 Negative control. A scanner that has never fired is indistinguishable
    # from a scanner that cannot fire, so plant both violations it is meant to
    # catch and confirm it catches them.
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp)
        (planted / "sweep.py").write_text(
            "import cfg\nx = [p for p in cfg.ARTIFACT_ROOT.rglob('*.parquet')]\n",
            encoding="utf-8",
        )
        (planted / "peek.py").write_text(
            "import cfg, pandas as pd\n"
            "d = pd.read_parquet(cfg.HOLDOUT_DIR / 'market=us')\n",
            encoding="utf-8",
        )
        caught = holdout_gate.assert_no_unguarded_holdout_access(planted)
        joined = " ".join(caught)
        check("scanner catches a planted artifact-root sweep", "sweep.py" in joined)
        check("scanner catches a planted holdout read", "peek.py" in joined)

    print("\n== Survivorship label enforcement ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sample = pd.DataFrame(
            {
                "symbol": ["AAA"],
                "session_date": [pd.Timestamp("2016-01-04")],
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [10],
            }
        )
        labeled = root / "labeled.parquet"
        write_labeled_parquet(sample, labeled, root=root)
        check("written partition carries the label", verify_label(labeled))

        # An unlabelled file written the ordinary way must be REFUSED, and the
        # refusal must be an exception — a silent filter or empty frame would
        # let a consumer proceed unaware, which is the failure being fixed.
        plain = root / "plain.parquet"
        sample.to_parquet(plain, index=False)
        check("unlabelled file is detected", not verify_label(plain))
        rejected = False
        try:
            read_labeled_parquet(plain)
        except UnlabeledCorpusError:
            rejected = True
        check("loader REFUSES unlabelled read", rejected)
        returned = read_labeled_parquet(labeled)
        check(
            "loader returns rows for a labelled file",
            len(returned) == 1,
            "refusal is targeted, not blanket",
        )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
