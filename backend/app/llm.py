"""Backend import shim for the shared pipeline LLM client."""
from __future__ import annotations

import sys
from pathlib import Path

PIPELINE_SCRIPTS = Path(__file__).resolve().parents[2] / "pipeline" / "scripts"
if str(PIPELINE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PIPELINE_SCRIPTS))

from llm_client import (  # noqa: E402,F401
    command_configured,
    complete,
    complete_command,
    configured,
    identity,
    model,
    provider,
)
