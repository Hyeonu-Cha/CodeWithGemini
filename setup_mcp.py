"""Regenerates .mcp.json with the Python and gemini binaries active in this environment.

Run via setup.bat (Windows) or: python setup_mcp.py
"""
import json
import pathlib
import shutil
import sys

script_dir = pathlib.Path(__file__).parent.resolve()

python_exe = sys.executable
gemini_bin = shutil.which("gemini")

if not gemini_bin:
    print("ERROR: gemini CLI not found on PATH.")
    print("Install it with: npm install -g @google/gemini-cli")
    sys.exit(1)

config = {
    "mcpServers": {
        "gemini-builder": {
            "command": python_exe,
            "args": ["-m", "gemini_mcp"],
            "cwd": str(script_dir),
        }
    }
}

out = script_dir / ".mcp.json"
# Force utf-8: on Windows, write_text falls back to cp1252 which can mangle
# paths containing non-ASCII characters (e.g. localized usernames).
out.write_text(json.dumps(config, indent=2), encoding="utf-8")
print("Written .mcp.json")
print(f"  python : {python_exe}")
print(f"  gemini : {gemini_bin}")
