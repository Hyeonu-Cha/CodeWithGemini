@echo off
:: Regenerates .mcp.json with the Python and gemini binaries that are currently
:: active in this environment. Run this once after cloning, or after changing
:: your Python installation.
python "%~dp0setup_mcp.py"
