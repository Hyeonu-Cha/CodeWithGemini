# Legacy thin entry point — only needed if running `python server.py` directly.
# The MCP server is normally started via: python -m gemini_mcp  (see .mcp.json)
import gemini_mcp.tools  # noqa: F401 — registers @mcp.tool decorators
from gemini_mcp import mcp

if __name__ == "__main__":
    mcp.run()
