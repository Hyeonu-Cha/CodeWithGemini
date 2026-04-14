#!/bin/sh
# Regenerates .mcp.json with the Python and gemini binaries that are currently
# active in this environment. Run this once after cloning, or after changing
# your Python installation.
python3 "$(dirname "$0")/setup_mcp.py"
