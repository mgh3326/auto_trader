"""Executable shim for the default-disabled ROB-1040 CRS-24 CLI."""

from __future__ import annotations

from rob1040_crs24_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
