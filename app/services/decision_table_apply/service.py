"""Apply a validated decision table through existing local persistence writers.

This service intentionally has no broker client dependency.  It coordinates
already-committing MCP writers rather than pretending their independent
transactions can be rolled back.  A separate analysis artifact stores durable
per-scenario markers so a later invocation can resume safely.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.services.decision_table_validate import decision_table_validate

ApplyCallable = Callable[..., Awaitable[dict[str, Any]]]

_APPLY_RECORD_SCHEMA = "kr-nxt-apply-record/v1"
_MARKET_TO_ORDER_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}
_MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}
_ACTION_KINDS = {
    "proposal": "proposal",
    "order": "proposal",
    "order_proposal": "proposal",
    "watch": "watch",
    "forecast": "forecast",
}


@dataclass(frozen=True)
class DecisionTableApplyDependencies:
    """Writer boundary injected by the MCP registration or focused tests.

    Each dependency is an existing MCP implementation.  Keeping the boundary
    callable-based prevents this coordinator from opening a shared session or
    importing a broker client.
    """

    artifact_get: ApplyCallable
    artifact_list: ApplyCallable
    artifact_save: ApplyCallable
    proposal_create: ApplyCallable
    watch_create: ApplyCallable
    forecast_save: ApplyCallable
    context_append: ApplyCallable


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _timestamp(now: datetime) -> str:
    return now.isoformat()


def _failure(error: str, **details: Any) -> dict[str, Any]:
    return {"success": False, "error": error, **details}


def _writer_error(response: object) -> str:
    if isinstance(response, dict) and isinstance(response.get("error"), str):
        return response["error"]
    return "writer_failed"


def _response_success(response: object) -> bool:
    return isinstance(response, dict) and response.get("success") is True


def _payload_envelope(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    payload = artifact.get("payload")
    if not isinstance(payload, dict) or not isinstance(
        payload.get("decision_table"), dict
    ):
        return None
    return payload


def _apply_date(envelope: dict[str, Any]) -> str | None:
    trading_date = envelope.get("trading_date")
    if isinstance(trading_date, str) and len(trading_date) == 10:
        try:
            datetime.strptime(trading_date, "%Y-%m-%d")
        except ValueError:
            return None
        return trading_date

    correlation_id = envelope.get("correlation_id")
    prefix = "kr-nxt-prep-"
    if isinstance(correlation_id, str) and correlation_id.startswith(prefix):
        candidate = correlation_id.removeprefix(prefix)
        if len(candidate) == 10:
            try:
                datetime.strptime(candidate, "%Y-%m-%d")
            except ValueError:
                return None
            return candidate
    return None


def _scenario_id(row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    value = row.get("scenario_id")
    return value if isinstance(value, str) and value else None


def _record_marker_id(marker: object) -> str | None:
    if not isinstance(marker, dict):
        return None
    for field in ("proposal_id", "watch_id", "forecast_id"):
        value = marker.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _valid_marker(marker: object) -> bool:
    return _record_marker_id(marker) is not None and isinstance(
        marker.get("at") if isinstance(marker, dict) else None, str
    )


def _apply_record_payload(
    *,
    parent_artifact_uuid: str,
    table_hash: str,
    rows: dict[str, dict[str, str]],
    complete: bool,
    at: str,
) -> dict[str, Any]:
    return {
        "schema": _APPLY_RECORD_SCHEMA,
        "parent_artifact_uuid": parent_artifact_uuid,
        "table_hash": table_hash,
        "rows": rows,
        "complete": complete,
        "at": at,
    }


def _matching_apply_record(
    payload: object, *, parent_artifact_uuid: str, table_hash: str
) -> tuple[dict[str, dict[str, str]], bool] | None:
    """Return markers and complete only for an exact identity.

    A differently hashed table is deliberately not an error and is not a
    resume target.  A malformed record for the exact identity is surfaced by
    the caller as a fail-closed coordination error rather than silently
    reapplying a potentially completed row.
    """

    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != _APPLY_RECORD_SCHEMA:
        return None
    if (
        payload.get("parent_artifact_uuid") != parent_artifact_uuid
        or payload.get("table_hash") != table_hash
    ):
        return None
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, dict) or not isinstance(payload.get("complete"), bool):
        raise ValueError("apply_record_invalid")
    rows: dict[str, dict[str, str]] = {}
    for scenario, marker in raw_rows.items():
        if not isinstance(scenario, str) or not _valid_marker(marker):
            raise ValueError("apply_record_invalid")
        rows[scenario] = dict(marker)
    return rows, payload["complete"]


async def _load_apply_record(
    dependencies: DecisionTableApplyDependencies,
    *,
    correlation_id: str,
    parent_artifact_uuid: str,
    table_hash: str,
) -> tuple[dict[str, dict[str, str]], bool, str | None] | dict[str, Any]:
    """Follow list (metadata) -> newest get (payload) exactly once."""

    try:
        listed = await dependencies.artifact_list(
            correlation_id=correlation_id,
            include_stale=True,
            limit=1,
        )
    except Exception:  # noqa: BLE001 - writer boundary failure has a stable code
        return _failure("apply_record_unavailable")
    if not _response_success(listed):
        return _failure("apply_record_unavailable")
    artifacts = listed.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return {}, False, None
    newest = artifacts[0]
    if not isinstance(newest, dict):
        return _failure("apply_record_invalid")
    record_uuid = newest.get("artifact_uuid")
    if not isinstance(record_uuid, str) or not record_uuid:
        return _failure("apply_record_invalid")
    try:
        fetched = await dependencies.artifact_get(record_uuid)
    except Exception:  # noqa: BLE001 - writer boundary failure has a stable code
        return _failure("apply_record_unavailable")
    if not _response_success(fetched):
        return _failure("apply_record_unavailable")
    record = fetched.get("artifact")
    payload = record.get("payload") if isinstance(record, dict) else None
    try:
        matched = _matching_apply_record(
            payload,
            parent_artifact_uuid=parent_artifact_uuid,
            table_hash=table_hash,
        )
    except ValueError:
        return _failure("apply_record_invalid")
    if matched is None:
        return {}, False, None
    rows, complete = matched
    return rows, complete, record_uuid


def _all_symbols(rows: list[dict[str, Any]]) -> list[str]:
    symbols: set[str] = set()
    for row in rows:
        raw_symbols = row.get("symbols")
        if isinstance(raw_symbols, list):
            symbols.update(
                item for item in raw_symbols if isinstance(item, str) and item
            )
    return sorted(symbols)


async def _save_apply_record(
    dependencies: DecisionTableApplyDependencies,
    *,
    market: str,
    correlation_id: str,
    parent_artifact_uuid: str,
    table_hash: str,
    rows: dict[str, dict[str, str]],
    complete: bool,
    symbols: list[str],
    now: datetime,
) -> dict[str, Any]:
    payload = _apply_record_payload(
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
        rows=rows,
        complete=complete,
        at=_timestamp(now),
    )
    try:
        result = await dependencies.artifact_save(
            market=market,
            kind="session_summary",
            title=f"Decision table apply {table_hash[:12]}",
            symbols=symbols,
            payload=payload,
            as_of=_timestamp(now),
            created_by="system",
            session_label="decision_table_apply",
            correlation_id=correlation_id,
        )
    except Exception:  # noqa: BLE001 - writer boundary failure has a stable code
        return _failure("apply_record_unavailable")
    if not _response_success(result):
        return _failure("apply_record_unavailable")
    artifact = result.get("artifact")
    record_uuid = artifact.get("artifact_uuid") if isinstance(artifact, dict) else None
    return {"success": True, "apply_record_uuid": record_uuid}


def _action_kind(action: object) -> str | None:
    if not isinstance(action, dict):
        return None
    raw_kind = action.get(
        "apply_kind",
        action.get("kind", action.get("action_type", action.get("type", "proposal"))),
    )
    return _ACTION_KINDS.get(raw_kind) if isinstance(raw_kind, str) else None


def _only_symbol(row: dict[str, Any]) -> str | None:
    symbols = row.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != 1:
        return None
    symbol = symbols[0]
    return symbol if isinstance(symbol, str) and symbol else None


def _rung_mapping(action: dict[str, Any]) -> list[dict[str, str | int | None]]:
    """Translate v1.1 pinned integer rungs to the proposal writer contract."""

    side = action.get("side")
    rungs = action.get("rungs")
    if side not in {"buy", "sell"} or not isinstance(rungs, list) or not rungs:
        raise ValueError("invalid_proposal_rungs")
    mapped: list[dict[str, str | int | None]] = []
    for rung in rungs:
        if not isinstance(rung, dict):
            raise ValueError("invalid_proposal_rungs")
        index = rung.get("rung")
        price_min = rung.get("price_min")
        price_max = rung.get("price_max")
        quantity = rung.get("qty")
        if (
            type(index) is not int
            or type(price_min) is not int
            or type(price_max) is not int
            or type(quantity) is not int
            or price_min != price_max
        ):
            # Validation should have stopped malformed v1.1 rows, but never
            # turn a pinned-range violation into a proposal anyway.
            raise ValueError("pinned_price_required")
        mapped.append(
            {
                "rung_index": index,
                "side": side,
                "quantity": str(quantity),
                "limit_price": str(price_min),
                "notional": str(price_min * quantity),
            }
        )
    return mapped


def _provenance(
    *, parent_artifact_uuid: str, table_hash: str, scenario_id: str
) -> dict[str, dict[str, str]]:
    return {
        "decision_table_apply": {
            "parent_artifact_uuid": parent_artifact_uuid,
            "table_hash": table_hash,
            "scenario_id": scenario_id,
        }
    }


def _stable_key(*, parent_artifact_uuid: str, table_hash: str, scenario_id: str) -> str:
    seed = f"{parent_artifact_uuid}:{table_hash}:{scenario_id}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _proposal_kwargs(
    row: dict[str, Any],
    *,
    market: str,
    parent_artifact_uuid: str,
    table_hash: str,
    scenario_id: str,
) -> dict[str, Any]:
    action = row.get("action")
    if not isinstance(action, dict):
        raise ValueError("invalid_action")
    symbol = _only_symbol(row)
    if symbol is None:
        raise ValueError("proposal_requires_one_symbol")
    proposal_action = action.get("proposal_action")
    account_mode = action.get("account_mode")
    side = action.get("side")
    order_type = action.get("order_type")
    if (
        not isinstance(proposal_action, str)
        or not isinstance(account_mode, str)
        or not isinstance(side, str)
        or not isinstance(order_type, str)
    ):
        raise ValueError("invalid_action")
    kwargs: dict[str, Any] = {
        "symbol": symbol,
        # The proposal writer accepts the public aliases, but making the
        # kr->equity_kr mapping explicit at this boundary preserves the apply
        # contract even if aliases change later.
        "market": _MARKET_TO_ORDER_MARKET[market],
        "account_mode": account_mode,
        "side": side,
        "order_type": order_type,
        "proposer": "decision_table_apply",
        "rungs": _rung_mapping(action),
        "thesis": f"decision table scenario {scenario_id}",
        "rationale": _provenance(
            parent_artifact_uuid=parent_artifact_uuid,
            table_hash=table_hash,
            scenario_id=scenario_id,
        ),
        "action": proposal_action,
    }
    for source, target in (
        ("valid_until", "valid_until"),
        ("supersedes_proposal_id", "supersedes_proposal_id"),
        ("exit_intent", "exit_intent"),
        ("exit_reason", "exit_reason"),
        ("retrospective_id", "retrospective_id"),
        ("approval_issue_id", "approval_issue_id"),
        ("target_broker_order_id", "target_broker_order_id"),
    ):
        if source in action:
            kwargs[target] = action[source]
    return kwargs


def _watch_kwargs(
    row: dict[str, Any],
    *,
    market: str,
    parent_artifact_uuid: str,
    table_hash: str,
    scenario_id: str,
) -> dict[str, Any]:
    action = row.get("action")
    if not isinstance(action, dict):
        raise ValueError("invalid_action")
    config = action.get("watch", action.get("watch_config"))
    if not isinstance(config, dict):
        raise ValueError("watch_config_required")
    symbol = _only_symbol(row)
    if symbol is None:
        raise ValueError("watch_requires_one_symbol")
    required = ("intent", "rationale", "watch_condition", "valid_until")
    if any(field not in config for field in required):
        raise ValueError("watch_config_required")
    key = _stable_key(
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
        scenario_id=scenario_id,
    )
    metadata = config.get("metadata")
    merged_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    merged_metadata["decision_table_apply"] = _provenance(
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
        scenario_id=scenario_id,
    )["decision_table_apply"]
    return {
        "created_by": "decision_table_apply",
        "market": market,
        "symbol": symbol,
        "intent": config["intent"],
        "rationale": config["rationale"],
        "watch_condition": config["watch_condition"],
        "valid_until": config["valid_until"],
        "trigger_checklist": config.get("trigger_checklist"),
        "max_action": config.get("max_action"),
        "metadata": merged_metadata,
        "idempotency_key": f"decision-table-apply-{key}",
    }


def _forecast_kwargs(
    row: dict[str, Any],
    *,
    market: str,
    parent_artifact_uuid: str,
    table_hash: str,
    scenario_id: str,
) -> dict[str, Any]:
    action = row.get("action")
    if not isinstance(action, dict):
        raise ValueError("invalid_action")
    config = action.get("forecast", action.get("forecast_config"))
    if not isinstance(config, dict):
        raise ValueError("forecast_config_required")
    symbol = _only_symbol(row)
    if symbol is None:
        raise ValueError("forecast_requires_one_symbol")
    required = ("forecast_target", "probability", "review_date")
    if any(field not in config for field in required):
        raise ValueError("forecast_config_required")
    seed = _stable_key(
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
        scenario_id=scenario_id,
    )
    kwargs: dict[str, Any] = {
        "created_by": "decision_table_apply",
        "symbol": symbol,
        "instrument_type": _MARKET_TO_INSTRUMENT[market],
        "forecast_target": config["forecast_target"],
        "probability": config["probability"],
        "review_date": config["review_date"],
        "forecast_id": str(uuid5(NAMESPACE_URL, f"decision-table-apply:{seed}")),
        "session_label": "decision_table_apply",
        "artifact_uuid": parent_artifact_uuid,
        "correlation_id": f"decision-table-apply-{seed[:24]}",
    }
    for field in (
        "horizon",
        "probability_range_low",
        "probability_range_high",
        "evidence_ids",
        "contrary_evidence",
        "forecast_start_date",
        "resolution_source",
        "model_label",
        "policy_version",
        "decision_bucket",
    ):
        if field in config:
            kwargs[field] = config[field]
    return kwargs


def _row_kwargs(
    row: dict[str, Any],
    *,
    kind: str,
    market: str,
    parent_artifact_uuid: str,
    table_hash: str,
    scenario_id: str,
) -> dict[str, Any]:
    kwargs_by_kind = {
        "proposal": _proposal_kwargs,
        "watch": _watch_kwargs,
        "forecast": _forecast_kwargs,
    }
    return kwargs_by_kind[kind](
        row,
        market=market,
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
        scenario_id=scenario_id,
    )


async def _apply_row(
    dependencies: DecisionTableApplyDependencies,
    row: dict[str, Any],
    *,
    kind: str,
    market: str,
    parent_artifact_uuid: str,
    table_hash: str,
    scenario_id: str,
) -> tuple[str, str | None, str | None]:
    try:
        kwargs = _row_kwargs(
            row,
            kind=kind,
            market=market,
            parent_artifact_uuid=parent_artifact_uuid,
            table_hash=table_hash,
            scenario_id=scenario_id,
        )
    except (KeyError, ValueError):
        return "failed", None, "invalid_row_mapping"
    writer = {
        "proposal": dependencies.proposal_create,
        "watch": dependencies.watch_create,
        "forecast": dependencies.forecast_save,
    }[kind]
    try:
        response = await writer(**kwargs)
    except Exception:  # noqa: BLE001 - keep later rows resumable
        return "failed", None, "writer_failed"
    if not _response_success(response):
        return "failed", None, _writer_error(response)
    if kind == "proposal":
        identifier = response.get("proposal_id")
    elif kind == "watch":
        alert = response.get("alert")
        identifier = alert.get("alert_uuid") if isinstance(alert, dict) else None
    else:
        data = response.get("data")
        identifier = data.get("forecast_id") if isinstance(data, dict) else None
    if not isinstance(identifier, str) or not identifier:
        return "failed", None, "writer_missing_identifier"
    return "applied", identifier, None


def _marker_for(kind: str, identifier: str, now: datetime) -> dict[str, str]:
    marker_field = {
        "proposal": "proposal_id",
        "watch": "watch_id",
        "forecast": "forecast_id",
    }[kind]
    return {marker_field: identifier, "at": _timestamp(now)}


async def _append_summary(
    dependencies: DecisionTableApplyDependencies,
    *,
    market: str,
    date: str,
    correlation_id: str,
    table_hash: str,
    symbols: list[str],
    applied: int,
    skipped: int,
    failed: int,
) -> bool:
    try:
        response = await dependencies.context_append(
            entries=[
                {
                    "kst_date": date,
                    "market": market,
                    "entry_type": "decision",
                    "title": "Decision table apply",
                    "body": (
                        f"table={table_hash[:12]} applied={applied} "
                        f"skipped={skipped} failed={failed}"
                    ),
                    "refs": {
                        "symbols": symbols,
                        "correlation_id": correlation_id,
                    },
                    "created_by": "system",
                    "session_label": "decision_table_apply",
                }
            ]
        )
    except Exception:  # noqa: BLE001 - row markers remain durable for retry
        return False
    return _response_success(response)


async def apply_decision_table(
    artifact_id: int | str,
    table_hash: str,
    *,
    dry_run: bool = True,
    confirm: bool = False,
    dependencies: DecisionTableApplyDependencies,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one exact validated artifact with durable idempotent resume.

    The six entry gates deliberately precede every writer.  After those gates,
    writers can fail independently; successful rows are marked immediately so
    the next call never creates a duplicate proposal/watch/forecast.
    """

    try:
        fetched = await dependencies.artifact_get(artifact_id)
    except Exception:  # noqa: BLE001 - stable MCP error, no write occurred
        return _failure("artifact_not_found")
    if not _response_success(fetched):
        return _failure("artifact_not_found")
    artifact = fetched.get("artifact")
    envelope = _payload_envelope(artifact)
    if envelope is None:
        return _failure("not_a_decision_table")

    market = envelope.get("market")
    if not isinstance(market, str) or market not in _MARKET_TO_ORDER_MARKET:
        return _failure("not_a_decision_table")
    payload_hash = envelope.get("decision_table_hash")
    validation = decision_table_validate(envelope, market)
    recomputed_hash = validation.get("recomputed", {}).get("hash")
    if not (
        isinstance(table_hash, str)
        and table_hash == payload_hash
        and table_hash == recomputed_hash
    ):
        return _failure(
            "table_hash_mismatch",
            argument_table_hash=table_hash,
            payload_table_hash=payload_hash,
            recomputed_table_hash=recomputed_hash,
        )
    if validation.get("valid") is not True:
        return _failure("table_invalid", violations=validation.get("violations", []))
    if not dry_run and confirm is not True:
        return _failure("confirm_required")

    parent_artifact_uuid = (
        artifact.get("artifact_uuid") if isinstance(artifact, dict) else None
    )
    if not isinstance(parent_artifact_uuid, str) or not parent_artifact_uuid:
        return _failure("not_a_decision_table")
    date = _apply_date(envelope)
    if date is None:
        return _failure("invalid_apply_date")
    correlation_id = f"kr-nxt-apply-{date}"
    loaded = await _load_apply_record(
        dependencies,
        correlation_id=correlation_id,
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
    )
    if isinstance(loaded, dict):
        return loaded
    markers, record_complete, apply_record_uuid = loaded
    if record_complete:
        return {
            "success": True,
            "already_applied": True,
            "complete": True,
            "already_applied_rows": sorted(markers),
            "apply_record_uuid": apply_record_uuid,
            "rows": [],
        }

    decision_table = envelope["decision_table"]
    raw_rows = decision_table.get("rows") if isinstance(decision_table, dict) else None
    if not isinstance(raw_rows, list):
        return _failure("table_invalid", violations=validation.get("violations", []))
    rows = [row for row in raw_rows if isinstance(row, dict)]
    symbols = _all_symbols(rows)
    results: list[dict[str, Any]] = []
    already_applied_rows: list[str] = []

    for row in rows:
        scenario_id = _scenario_id(row)
        if scenario_id is None:
            # The validator normally makes this impossible.  Keep the writer
            # boundary closed if a future validator contract changes.
            results.append(
                {"scenario_id": None, "status": "failed", "error": "invalid_row"}
            )
            continue
        marker = markers.get(scenario_id)
        if marker is not None:
            already_applied_rows.append(scenario_id)
            results.append(
                {
                    "scenario_id": scenario_id,
                    "status": "skipped",
                    "id": _record_marker_id(marker),
                }
            )
            continue
        action = row.get("action")
        kind = _action_kind(action)
        if kind is None:
            results.append(
                {
                    "scenario_id": scenario_id,
                    "status": "failed",
                    "error": "unsupported_apply_kind",
                }
            )
            continue
        if dry_run:
            try:
                _row_kwargs(
                    row,
                    kind=kind,
                    market=market,
                    parent_artifact_uuid=parent_artifact_uuid,
                    table_hash=table_hash,
                    scenario_id=scenario_id,
                )
            except (KeyError, ValueError):
                results.append(
                    {
                        "scenario_id": scenario_id,
                        "status": "failed",
                        "error": "invalid_row_mapping",
                    }
                )
            else:
                results.append(
                    {
                        "scenario_id": scenario_id,
                        "status": "skipped",
                        "reason": "dry_run",
                        "kind": kind,
                    }
                )
            continue

        status, identifier, error = await _apply_row(
            dependencies,
            row,
            kind=kind,
            market=market,
            parent_artifact_uuid=parent_artifact_uuid,
            table_hash=table_hash,
            scenario_id=scenario_id,
        )
        if status == "failed":
            results.append(
                {"scenario_id": scenario_id, "status": "failed", "error": error}
            )
            continue
        assert identifier is not None
        markers[scenario_id] = _marker_for(kind, identifier, now or _now())
        results.append(
            {
                "scenario_id": scenario_id,
                "status": "applied",
                "id": identifier,
                "kind": kind,
            }
        )
        saved = await _save_apply_record(
            dependencies,
            market=market,
            correlation_id=correlation_id,
            parent_artifact_uuid=parent_artifact_uuid,
            table_hash=table_hash,
            rows=markers,
            complete=False,
            symbols=symbols,
            now=now or _now(),
        )
        if saved.get("success") is not True:
            return {
                **saved,
                "complete": False,
                "rows": results,
                "already_applied_rows": already_applied_rows,
            }
        apply_record_uuid = saved.get("apply_record_uuid")

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "complete": False,
            "rows": results,
            "already_applied_rows": already_applied_rows,
        }

    applied = sum(item["status"] == "applied" for item in results)
    skipped = sum(item["status"] == "skipped" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    all_rows_marked = all(
        scenario_id is not None and scenario_id in markers
        for scenario_id in map(_scenario_id, rows)
    )
    summary_succeeded = await _append_summary(
        dependencies,
        market=market,
        date=date,
        correlation_id=correlation_id,
        table_hash=table_hash,
        symbols=symbols,
        applied=applied,
        skipped=skipped,
        failed=failed,
    )
    complete = all_rows_marked and summary_succeeded
    saved = await _save_apply_record(
        dependencies,
        market=market,
        correlation_id=correlation_id,
        parent_artifact_uuid=parent_artifact_uuid,
        table_hash=table_hash,
        rows=markers,
        complete=complete,
        symbols=symbols,
        now=now or _now(),
    )
    if saved.get("success") is not True:
        return {
            **saved,
            "complete": False,
            "rows": results,
            "already_applied_rows": already_applied_rows,
        }
    return {
        "success": True,
        "dry_run": False,
        "complete": complete,
        "rows": results,
        "already_applied_rows": already_applied_rows,
        "apply_record_uuid": saved.get("apply_record_uuid") or apply_record_uuid,
    }


__all__ = ["DecisionTableApplyDependencies", "apply_decision_table"]
