import logging
import logging.handlers
import os
import pathlib
import shutil

if not shutil.which("gemini"):
    raise RuntimeError(
        "gemini CLI not found on PATH. "
        "Install it with: npm install -g @google/gemini-cli"
    )

# Log to ~/.ccb/gemini-mcp.log — tail this file to watch live activity.
_log_dir = pathlib.Path.home() / ".ccb"
_log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(
        _log_dir / "gemini-mcp.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8",
    )],
)

_logger = logging.getLogger("gemini_mcp")

if not os.environ.get("GEMINI_MCP_ALLOWED_ROOT"):
    _logger.warning(
        "GEMINI_MCP_ALLOWED_ROOT is not set. "
        "Gemini's file tools (-y mode) can write to any absolute path, not just working_dir. "
        "Set GEMINI_MCP_ALLOWED_ROOT to restrict the starting directory, "
        "or run Gemini in a container for full isolation."
    )

import gemini_mcp.tools  # noqa: F401 — registers @mcp.tool decorators
from gemini_mcp import mcp

mcp.run()
