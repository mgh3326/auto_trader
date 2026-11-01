"""Report printer: net vs cash first, both bracket ends, markouts, fill rate last.

The printer re-checks the registry reference and the bracket ends so a report
object built around the constructor (``object.__new__``) still cannot print.
"""

from __future__ import annotations

from decimal import Decimal

from research.eval_contract.contract import (
    BRACKET_CONSERVATIVE,
    BRACKET_OPTIMISTIC,
    HORIZONS,
    ContractError,
    EvaluationReport,
    FillModelResult,
    MissingRegistryReference,
    Provenance,
    RegistryRef,
)

_ROLE = {
    BRACKET_OPTIMISTIC: "bracket: optimistic end",
    BRACKET_CONSERVATIVE: "bracket: conservative end",
}


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.4f}%"


def _rate(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _check(report: EvaluationReport) -> None:
    """Re-validate what the constructors check; a bypassed object cannot print."""

    provenance = getattr(report, "provenance", None)
    registry = getattr(provenance, "registry", None)
    if not isinstance(provenance, Provenance) or not isinstance(registry, RegistryRef):
        raise MissingRegistryReference("cannot print a report without a registry ref")
    try:
        RegistryRef(registry.hypothesis_id, registry.registry_hash, registry.k)
    except ContractError as exc:
        raise MissingRegistryReference(f"invalid registry ref: {exc}") from exc
    if registry.hypothesis_id != report.hypothesis_id:
        raise ContractError("report hypothesis differs from its registry entry")
    ids = {r.fill_model_id for r in report.results}
    for end in (BRACKET_OPTIMISTIC, BRACKET_CONSERVATIVE):
        if end not in ids:
            raise ContractError(f"cannot print a report without bracket end {end!r}")


def _row(r: FillModelResult) -> str:
    role = _ROLE.get(r.fill_model_id, "variant")
    if r.status != "ok":
        return (
            f"| {r.fill_model_id} | {role} | unavailable: {r.unavailable_reason} | | |"
        )
    return (
        f"| {r.fill_model_id} | {role} | {_pct(r.net_vs_cash)} | "
        f"{_money(r.terminal_value)} | {_money(r.cash_baseline)} |"
    )


def render_report(report: EvaluationReport) -> str:
    _check(report)
    p = report.provenance
    reg = p.registry
    assert reg is not None
    lines = [
        f"# Evaluation — {report.hypothesis_id} ({report.market.value})",
        "",
        f"- contract: {p.contract_version}",
        f"- registry: {reg.hypothesis_id} hash={reg.registry_hash} k={reg.k}",
        f"- code: {p.code_ref}",
        f"- data manifest sha256: {p.data_manifest_sha256}",
        f"- config sha256: {p.config_sha256}",
        "",
        "## Primary — net return vs cash at equal capital",
        "",
        "| fill model | role | net vs cash | terminal value | cash baseline |",
        "|---|---|---|---|---|",
    ]
    lines.extend(_row(r) for r in report.results)
    lo = report.result(BRACKET_CONSERVATIVE).net_vs_cash
    hi = report.result(BRACKET_OPTIMISTIC).net_vs_cash
    lines += [
        "",
        f"Bracket (conservative .. optimistic): {_pct(lo)} .. {_pct(hi)}",
        "",
        "## Markouts R_h — buy fills, net of costs, minus cash",
        "",
        "| fill model | h | n observed | n censored | mean net R_h | mean net vs cash |",
        "|---|---|---|---|---|---|",
    ]
    for r in report.results:
        if r.status != "ok":
            continue
        for h in HORIZONS:
            at_h = [o for o in r.outcomes if o.horizon == h]
            seen = [o for o in at_h if not o.censored]
            net = _mean([o.net_return for o in seen if o.net_return is not None])
            vs = _mean([o.net_vs_cash for o in seen if o.net_vs_cash is not None])
            lines.append(
                f"| {r.fill_model_id} | {h} | {len(seen)} | "
                f"{len(at_h) - len(seen)} | {_pct(net)} | {_pct(vs)} |"
            )
    lines += [
        "",
        "## Diagnostic — fill rate (not a performance metric)",
        "",
        "| fill model | orders | filled orders | fill rate |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        if r.status != "ok":
            lines.append(f"| {r.fill_model_id} | n/a | n/a | n/a |")
            continue
        lines.append(
            f"| {r.fill_model_id} | {len(r.orders)} | {len(r.filled_order_ids)} | "
            f"{_rate(r.fill_rate)} |"
        )
    lines += ["", f"report sha256: {report.sha256}", ""]
    return "\n".join(lines)
