"""External configuration for clinica_rpa (Regla 8: config via .env).

Phase 0 only needs logging-related settings; no real secrets are required
yet. This module shows the pattern (python-dotenv + pydantic) that later
phases will extend for real GO/Indigo connection settings.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load variables from a local .env file, if present, into the process
# environment. Safe to call multiple times; does not overwrite real
# environment variables that are already set.
load_dotenv()


class Settings(BaseModel):
    """Runtime configuration sourced from environment variables / .env."""

    log_level: str = Field(default="INFO")
    runtime_dir: Path = Field(default=Path("runtime"))


def get_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment.

    Reads ``LOG_LEVEL`` and ``RUNTIME_DIR`` if present (via ``.env`` or the
    real environment), falling back to sane defaults otherwise. No secrets
    are required for F0.1/F0.2.
    """
    return Settings(
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        runtime_dir=Path(os.getenv("RUNTIME_DIR", "runtime")),
    )
