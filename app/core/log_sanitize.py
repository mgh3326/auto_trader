import re
from typing import Any

_UNSAFE = re.compile(r"[^\w./\-:@]")
_MAX_LEN = 64


def safe_log_value(value: Any) -> str:
    """Sanitize a potentially user-controlled value for logging.

    Strips control chars and non-symbol characters, caps length. Returns
    a string suitable for inclusion in log format args.
    """
    s = str(value)
    cleaned = _UNSAFE.sub("_", s)
    if len(cleaned) > _MAX_LEN:
        cleaned = cleaned[:_MAX_LEN] + "..."
    return cleaned
