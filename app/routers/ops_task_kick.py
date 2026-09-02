"""Token-authenticated, static TaskIQ kick API for NCP workers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Path, status
from pydantic import ValidationError

from app.services.ops_task_kick.registry import TASK_KICK_REGISTRY
from app.services.ops_task_kick.service import (
    RedisUnavailableError,
    TaskDispatchUnavailableError,
    get_task_run,
    kick_task,
)

router = APIRouter(prefix="/trading/api/ops/tasks", tags=["ops-task-kick"])


@router.post("/{task_name}/kick", status_code=status.HTTP_202_ACCEPTED)
async def kick_registered_task(
    task_name: str,
    parameters: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> dict[str, Any]:
    spec = TASK_KICK_REGISTRY.get(task_name)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown task"
        )
    try:
        parsed_parameters = spec.parameters_model.model_validate(parameters)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(include_url=False),
        ) from exc
    try:
        task_id, deduplicated = await kick_task(
            task_name=task_name,
            spec=spec,
            parameters=parsed_parameters.model_dump(exclude_none=True),
            idempotency_key=idempotency_key,
        )
    except RedisUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task kick idempotency store unavailable",
        ) from exc
    except TaskDispatchUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task dispatch unavailable",
        ) from exc
    return {"task_id": task_id, "deduplicated": deduplicated}


@router.get("/runs/{task_id}")
async def get_registered_task_run(
    task_id: str = Path(min_length=1, max_length=128),
) -> dict[str, Any]:
    return await get_task_run(task_id)
