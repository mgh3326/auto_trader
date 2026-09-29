"""Stage 4 apply entrypoint. Run with uv run python and explicit arguments."""

from core import main

if __name__ == "__main__":
    raise SystemExit(main(4, "apply"))
