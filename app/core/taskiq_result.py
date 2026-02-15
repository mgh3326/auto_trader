from typing import Any

from taskiq.exceptions import ResultGetError, ResultIsReadyError

from app.core.taskiq_broker import broker


def _extract_error(task_result: Any) -> str:
    error = getattr(task_result, "error", None)
    if error is not None:
        return str(error)

    log = getattr(task_result, "log", None)
    if log:
        return str(log)

    return "Unknown task error"


async def build_task_status_response(task_id: str) -> dict[str, Any]:
    try:
        is_ready = await broker.result_backend.is_result_ready(task_id)
    except ResultIsReadyError:
        is_ready = False

    response: dict[str, Any] = {
        "task_id": task_id,
        "state": "PENDING",
        "ready": is_ready,
        "is_ready": is_ready,
    }

    if not is_ready:
        return response

    try:
        task_result = await broker.result_backend.get_result(task_id, with_logs=True)
    except ResultGetError as exc:
        response["state"] = "FAILURE"
        response["error"] = str(exc)
        return response

    if task_result.is_err:
        response["state"] = "FAILURE"
        response["error"] = _extract_error(task_result)
    else:
        response["state"] = "SUCCESS"
        response["result"] = task_result.return_value

    return response
