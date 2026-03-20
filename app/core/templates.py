"""Shared templates configuration for FastAPI routers."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

# Calculate templates directory path relative to this file
# app/core/templates.py -> app/core -> app -> templates
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Shared templates instance for all routers
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
