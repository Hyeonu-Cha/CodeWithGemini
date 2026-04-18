from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import platform
import re
import shutil
import time

from gemini_mcp.core.parsers import extract_json

logger = logging.getLogger("gemini_mcp.runner")

# Timeouts (seconds) — override via GEMINI_MCP_*_TIMEOUT env vars.
# Use float so fractional seconds (e.g. "30.5") don't crash at import time.
def _timeout_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using default %s", name, raw, default)
        return default


EXECUTE_TIMEOUT = _timeout_env("GEMINI_MCP_EXECUTE_TIMEOUT", 300.0)
REVIEW_TIMEOUT  = _timeout_env("GEMINI_MCP_REVIEW_TIMEOUT",  120.0)
PLAN_TIMEOUT    = _timeout_env("GEMINI_MCP_PLAN_TIMEOUT",    120.0)
PING_TIMEOUT    = _timeout_env("GEMINI_MCP_PING_TIMEOUT",     30.0)

# Resolve gemini binary once at import time so every call uses the full path.
_GEMINI_BIN = shutil.which("gemini") or "gemini"

# Optional: override the Gemini model (e.g. "gemini-2.5-pro").
_MODEL = os.environ.get("GEMINI_MCP_MODEL")

# Optional: restrict working_dir to a subtree (e.g. "C:\\Users\\gotow").
_ALLOWED_ROOT = os.environ.get("GEMINI_MCP_ALLOWED_ROOT")

# Retry prompt echoes the previous output head AND tail — JSON parse errors
# cluster near the end (unclosed brace, trailing prose), so head-only truncation
# is likely to hide the actual problem.
_RETRY_HEAD_CHARS = 500
_RETRY_TAIL_CHARS = 500


def _retry_snippet(output: str) -> str:
    """Return output unchanged if short; otherwise head + elision + tail."""
    total = _RETRY_HEAD_CHARS + _RETRY_TAIL_CHARS
    if len(output) <= total:
        return output
    head   = output[:_RETRY_HEAD_CHARS]
    tail   = output[-_RETRY_TAIL_CHARS:]
    elided = len(output) - total
    return f"{head}\n...[{elided} chars elided]...\n{tail}"


def _retry_suffix(prev_output: str, parse_error: str | None) -> str:
    """Build a retry instruction that includes the last failing output.

    LLMs fix JSON errors far more reliably when shown what they said previously
    AND a pointer to the specific parse failure, so they can correct the mistake
    rather than regenerating from scratch.
    """
    snippet    = _retry_snippet(prev_output)
    error_hint = f"Parse failure: {parse_error}\n\n" if parse_error else ""
    return (
        "\n\nIMPORTANT: Your previous response (shown below) could not be parsed as JSON. "
        "Respond with ONLY the JSON object — no explanation, no markdown fences, no extra text. "
        "Fix the specific problem flagged below and return a corrected version.\n\n"
        f"{error_hint}"
        f"--- PREVIOUS RESPONSE ---\n{snippet}\n--- END PREVIOUS RESPONSE ---"
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


async def _run_subprocess(
    cmd: str | list[str],
    use_shell: bool,
    prompt: str,
    working_dir: str | None,
    timeout: float,
) -> tuple[str, str, int]:
    """Spawn Gemini CLI, pipe prompt via stdin, return (stdout, stderr, returncode).

    Isolated into its own coroutine so tests can patch it without touching
    asyncio internals. Kills the process if the timeout fires.
    """
    input_bytes = prompt.encode("utf-8")

    if use_shell:
        proc = await asyncio.create_subprocess_shell(
            cmd,  # type: ignore[arg-type]
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd,  # type: ignore[arg-type]
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise

    return (
        stdout_b.decode("utf-8", errors="replace").strip(),
        stderr_b.decode("utf-8", errors="replace").strip(),
        proc.returncode or 0,
    )


async def run_gemini(prompt: str, working_dir: str | None = None, timeout: float = 120) -> str:
    """Pipe prompt to Gemini CLI via stdin and return its JSON response.

    Retries once with a stricter suffix if the first response is not valid JSON.
    Non-blocking — uses asyncio subprocess so the MCP server stays responsive
    during long-running Gemini calls.
    """
    if working_dir is not None:
        try:
            validate_working_dir(working_dir)
        except ValueError as exc:
            return json.dumps({"errorType": "validationError", "error": str(exc)})

    cmd, use_shell = _make_cmd()
    start            = time.monotonic()
    attempt          = 0
    prev_output      = ""
    prev_parse_error: str | None = None

    try:
        for attempt in range(2):  # attempt 0 = initial, attempt 1 = retry
            current_prompt = (
                prompt if attempt == 0
                else prompt + _retry_suffix(prev_output, prev_parse_error)
            )

            output, stderr, returncode = await _run_subprocess(
                cmd, use_shell, current_prompt, working_dir, timeout,
            )
            elapsed = time.monotonic() - start

            # Any non-zero exit is treated as failure — even if stdout has text.
            # Parsing stdout from a failing process can mask auth/quota errors
            # that happen to print partial JSON before exit.
            if returncode != 0:
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
                    "geminiError code=%d attempt=%d elapsed=%.1fs stderr=%s stdout=%.200s",
                    returncode, attempt + 1, elapsed, stderr[:200], output,
                )
                return json.dumps({
                    "errorType": "geminiError",
                    "error": f"Gemini exited with code {returncode}: {stderr}",
                })

            try:
                parsed = extract_json(output)
                logger.info(
                    "run_gemini ok attempt=%d elapsed=%.1fs prompt_len=%d",
                    attempt + 1, elapsed, len(prompt),
                )
                return parsed
            except ValueError as exc:
                # JSONDecodeError (subclass of ValueError) carries pos/line/col,
                # which we thread into the retry prompt so the model knows
                # exactly where it went wrong rather than having to rediscover.
                if isinstance(exc, json.JSONDecodeError):
                    parse_error = f"{exc.msg} at line {exc.lineno} column {exc.colno} (char {exc.pos})"
                else:
                    parse_error = str(exc)

                if attempt == 1:  # exhausted retries
                    logger.warning(
                        "parseError after retry elapsed=%.1fs err=%s rawOutput=%.100s",
                        elapsed, parse_error, output,
                    )
                    return json.dumps({
                        "errorType": "parseError",
                        "error": "Gemini did not return valid JSON after retry.",
                        "parseError": parse_error,
                        "rawOutput": output[:500],
                    })
                logger.info("parseError on attempt 1 (%s) — retrying with echo", parse_error)
                prev_output      = output       # for _retry_suffix on next iteration
                prev_parse_error = parse_error  # surfaced to Gemini as a pointer
                # fall through to retry

        # Defensive fallthrough: every branch above should return within the
        # loop, so reaching here means a future refactor broke that invariant.
        # Explicit runError keeps the caller contract intact (always JSON).
        logger.error("run_gemini fell through retry loop without returning")
        return json.dumps({
            "errorType": "runError",
            "error": "run_gemini exited retry loop without returning",
        })

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        logger.warning("timeout attempt=%d elapsed=%.1fs limit=%ss", attempt + 1, elapsed, timeout)
        return json.dumps({
            "errorType": "timeout",
            "error": f"Gemini did not respond within {timeout}s (attempt {attempt + 1})",
        })
    except Exception as exc:
        logger.exception("runError: %s", exc)
        return json.dumps({"errorType": "runError", "error": str(exc)})
