"""Write-once daily snapshots, isolated by market, and recoverable report publication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode()


def publish(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("immutable artifact conflict")
        return
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".staging-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(name)


@contextmanager
def locked(root: Path, market: str):
    root.mkdir(parents=True, exist_ok=True)
    with (root / f".{market}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def latest(root: Path, market: str) -> tuple[dict | None, str | None]:
    previous, previous_hash = None, None
    for path in sorted((root / "ledger" / market).glob("*.json")):
        value = json.loads(path.read_bytes())
        if value["previous_sha256"] != previous_hash or path.stem != value["asof"]:
            raise ValueError("ledger chain mismatch")
        previous, previous_hash = value, hashlib.sha256(path.read_bytes()).hexdigest()
    return previous, previous_hash


def markdown(report: dict) -> str:
    lines = [
        f"# Paper forward shadow: {report['market']} {report['asof']}",
        "",
        f"Input state: {report['input_state']}. Mode: {report['mode']}.",
        f"Ledger committed: {str(report.get('committed', False)).lower()}.",
        f"Sessions with incomplete input coverage: {', '.join(report.get('coverage_gaps', [])) or 'none'}.",
        "Hypothetical fills only. Costs use the research conservative round-trip reserve.",
        "D_i = (candidate P&L - control P&L) / fixed episode E; cash earns zero.",
        "This is a function check; no statistical promotion verdict.",
        "",
        f"Signals: {len(report['signals'])}; opened: {report['summary']['opened']}; added: {report['summary']['added']}; closed: {report['summary']['closed']}.",
        "",
        "| Episode | State | u | E | Open cost/E | Open value/E | P&L | H | Comparison | D_i |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def value(v):
        return "unverified" if v is None else str(v)

    for row in report["episode_report"]:
        result = row["h_result"] or {}
        lines.append(
            "| "
            + " | ".join(
                value(v)
                for v in (
                    row["id"],
                    row["state"],
                    row["u"],
                    row["E"],
                    row["open_cost_over_E"],
                    row["open_value_over_E"],
                    row["pnl"],
                    row["h_day"],
                    row["comparison_state"],
                    result.get("D_i"),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Signals and ledger diff:",
            "",
            "```json",
            json.dumps(
                {
                    "signals": report["signals"],
                    "ledger_diff": report["ledger_diff"],
                    "data_gaps": report["data_gaps"],
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def reports(root: Path, report: dict) -> tuple[Path, Path]:
    public = {k: v for k, v in report.items() if k not in {"episodes", "seen"}}
    verified = report["input_state"] == "verified"
    directory = (
        root
        / (
            "preview"
            if report["mode"] == "dry-run"
            else "reports"
            if verified
            else "unverified"
        )
        / report["market"]
    )
    name = (
        report["asof"]
        if verified and report["mode"] != "dry-run"
        else report["asof"] + "-" + hashlib.sha256(json_bytes(public)).hexdigest()[:16]
    )
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    publish(json_path, json_bytes(public))
    publish(md_path, markdown(public).encode())
    return md_path, json_path
