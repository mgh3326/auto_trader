"""Stage 3 rollback entrypoint. Run with uv run python and explicit arguments."""

from core import main

if __name__ == "__main__":
    raise SystemExit(main(3, "rollback"))
