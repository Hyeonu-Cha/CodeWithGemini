from __future__ import annotations

import json
import logging
import os
import pathlib
import platform
import re
import shutil
import subprocess
import time

from gemini_mcp.core.parsers import extract_json

logger = logging.getLogger("gemini_mcp.runner")

# Timeouts (seconds)
EXECUTE_TIMEOUT = 300   # 5 min — Gemini creates/edits files
REVIEW_TIMEOUT  = 120   # 2 min
PLAN_TIMEOUT    = 120   # 2 min

# Resolve gemini binary once at import time so every call uses the full path.
_GEMINI_BIN = shutil.which("gemini") or "gemini"

# Optional: override the Gemini model (e.g. "gemini-2.5-pro").
_MODEL = os.environ.get("GEMINI_MCP_MODEL")

# Optional: restrict working_dir to a subtree (e.g. "C:\\Users\\gotow").
_ALLOWED_ROOT = os.environ.get("GEMINI_MCP_ALLOWED_ROOT")

_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your previous response was not valid JSON. "
    "Respond with ONLY the JSON object — no explanation, no markdown fences, no extra text."
)

# Stderr substrings (case-insensitive) that indicate an auth/credential failure.
_AUTH_PATTERNS = (
    "unauthenticated",
    "invalid_grant",
    "token has been expired",
    "token expired",
    "not logged in",
    "please run",           # "Please run 'gemini' to authenticate"
    "credentials",
    "unauthorized",
    "401",
)


def _is_auth_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(p in lower for p in _AUTH_PATTERNS)


def validate_working_dir(wd: str) -> None:
    """Raise ValueError if wd is missing, not a directory, or outside the allowed root."""
    p = pathlib.Path(wd).resolve()
    if not p.exists():
        raise ValueError(f"working_dir does not exist: {wd}")
    if not p.is_dir():
        raise ValueError(f"working_dir is not a directory: {wd}")
    if _ALLOWED_ROOT:
        root = pathlib.Path(_ALLOWED_ROOT).resolve()
        if p != root and root not in p.parents:
            raise ValueError(
                f"working_dir '{wd}' is outside allowed root '{_ALLOWED_ROOT}'. "
                "Update GEMINI_MCP_ALLOWED_ROOT if this path is intentional."
            )


_MODEL_SAFE_RE = re.compile(r'^[\w.\-]+$')  # allowlist: alphanumeric, dots, hyphens only


def _validated_model() -> str | None:
    """Return _MODEL if it passes the allowlist, None otherwise.

    Rejects anything with shell metacharacters so _MODEL can never escape
    the quoted argument on the Windows shell=True command string.
    """
    if not _MODEL:
        return None
    if _MODEL_SAFE_RE.match(_MODEL):
        return _MODEL
    logger.warning(
        "GEMINI_MCP_MODEL value %r contains disallowed characters and will be ignored. "
        "Only alphanumeric characters, dots, and hyphens are permitted.",
        _MODEL,
    )
    return None


def _make_cmd() -> tuple[str | list[str], bool]:
    """Return (cmd, use_shell) appropriate for the current platform.

    Windows requires shell=True to resolve .cmd files (gemini.cmd).
    On all other platforms shell=False is used — safer and more portable.
    _GEMINI_BIN is the only data interpolated into the shell string and it
    comes from shutil.which(), not from user input. _MODEL is validated
    against an allowlist before interpolation.
    """
    model = _validated_model()
    model_args = ["-m", model] if model else []
    if platform.system() == "Windows":
        extra = f' -m "{model}"' if model else ""
        return f'"{_GEMINI_BIN}" -p " " -o text -y{extra}', True
    return [_GEMINI_BIN, "-p", " ", "-o", "text", "-y"] + model_args, False


def run_gemini(prompt: str, working_dir: str | None = None, timeout: int = 120) -> str:
    """Pipe prompt to Gemini CLI via stdin and return its JSON response.

    Retries once with a stricter suffix if the first response is not valid JSON.
    """
    if working_dir is not None:
        try:
            validate_working_dir(working_dir)
        except ValueError as exc:
            return json.dumps({"errorType": "validationError", "error": str(exc)})

    cmd, use_shell = _make_cmd()
    start   = time.monotonic()
    attempt = 0

    try:
        for attempt in range(2):  # attempt 0 = initial, attempt 1 = retry
            current_prompt = prompt if attempt == 0 else prompt + _RETRY_SUFFIX

            result = subprocess.run(
                cmd,
                shell=use_shell,
                input=current_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",   # explicit — avoids cp1252 crash on Windows
                timeout=timeout,
                cwd=working_dir,
            )
            output  = result.stdout.strip()
            elapsed = time.monotonic() - start

            if result.returncode != 0 and not output:
                stderr = result.stderr.strip()
                if _is_auth_error(stderr):
                    logger.warning("authExpired attempt=%d elapsed=%.1fs", attempt + 1, elapsed)
                    return json.dumps({
                        "errorType": "authExpired",
                        "error": (
                            "Gemini authentication expired or invalid. "
                            "Run 'gemini' interactively to re-authenticate."
                        ),
                        "stderr": stderr[:300],
                    })
                logger.warning(
                    "geminiError code=%d attempt=%d elapsed=%.1fs stderr=%s",
                    result.returncode, attempt + 1, elapsed, stderr[:200],
                )
                return json.dumps({
                    "errorType": "geminiError",
                    "error": f"Gemini exited with code {result.returncode}: {stderr}",
                })

            try:
                parsed = extract_json(output)
                logger.info(
                    "run_gemini ok attempt=%d elapsed=%.1fs prompt_len=%d",
                    attempt + 1, elapsed, len(prompt),
                )
                return parsed
            except ValueError:
                if attempt == 1:  # exhausted retries
                    logger.warning(
                        "parseError after retry elapsed=%.1fs rawOutput=%.100s",
                        elapsed, output,
                    )
                    return json.dumps({
                        "errorType": "parseError",
                        "error": "Gemini did not return valid JSON after retry.",
                        "rawOutput": output[:500],
                    })
                logger.info("parseError on attempt 1 — retrying with stricter prompt")
                # fall through to retry

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        logger.warning("timeout attempt=%d elapsed=%.1fs limit=%ds", attempt + 1, elapsed, timeout)
        return json.dumps({
            "errorType": "timeout",
            "error": f"Gemini did not respond within {timeout}s (attempt {attempt + 1})",
        })
    except Exception as exc:
        logger.exception("runError: %s", exc)
        return json.dumps({"errorType": "runError", "error": str(exc)})
