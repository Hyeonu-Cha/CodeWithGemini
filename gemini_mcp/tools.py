from __future__ import annotations

import json
import logging
import pathlib
import re

from gemini_mcp import mcp
from gemini_mcp.core.parsers import truncate, MAX_DIFF_CHARS, MAX_OUTPUT_CHARS, MAX_SPEC_CHARS
from gemini_mcp.core.runner import (
    run_gemini,
    EXECUTE_TIMEOUT,
    PLAN_TIMEOUT,
    REVIEW_TIMEOUT,
    PING_TIMEOUT,
)

logger = logging.getLogger("gemini_mcp.tools")

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"

# Required top-level keys for each tool's response.
_EXECUTE_KEYS = {"status", "filesCreated", "filesModified", "commandsRun", "summary", "issues"}
_REVIEW_KEYS  = {"verdict", "doneConditionsMet", "issues", "summary"}
_PLAN_KEYS    = {"taskName", "objective", "steps", "finalDone"}


def _load_prompt(name: str, **kwargs) -> str:
    """Load a prompt template and fill <<<varname>>> placeholders in a single pass.

    Single-pass regex replacement prevents a value containing <<<other>>>
    from being re-processed by a subsequent iteration.
    """
    text = (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    if not kwargs:
        return text
    table   = {f"<<<{k}>>>": str(v) for k, v in kwargs.items()}
    pattern = re.compile("|".join(re.escape(k) for k in table))
    result = pattern.sub(lambda m: table[m.group()], text)
    remaining = re.findall(r"<<<\w+>>>", result)
    if remaining:
        logger.warning("_load_prompt(%r): unfilled placeholders after substitution: %s", name, remaining)
    return result


def _inject_schema_warning(data: dict, required: set[str], tool: str) -> None:
    """Add a _schemaWarning key if any expected top-level keys are missing."""
    missing = required - data.keys()
    if missing:
        data["_schemaWarning"] = f"{tool}: missing expected keys: {sorted(missing)}"


def _finalize(result: str, required: set[str], tool: str) -> str:
    """Parse result JSON, inject schema warning if needed, return serialized."""
    try:
        data = json.loads(result)
        if "errorType" not in data:
            _inject_schema_warning(data, required, tool)
        return json.dumps(data)
    except json.JSONDecodeError:
        return result  # already an error string — pass through unchanged


@mcp.tool(description=(
    "Send Claude's architectural spec to Gemini for autonomous execution. "
    "Gemini uses its built-in file tools (-y mode) to create and modify files "
    "in working_dir as specified. "
    "Returns a structured execution report: status, filesCreated, filesModified, "
    "commandsRun, summary, issues. Feed this report directly into gemini_review."
))
async def gemini_execute(
    spec: str,
    working_dir: str,
    context_files: str | None = None,
) -> str:
    """
    spec          – Claude's complete architectural spec (what to build and how).
    working_dir   – Absolute path to the project directory where files will be created.
    context_files – Comma-separated existing file paths Gemini should read first (optional).
    """
    logger.info("gemini_execute working_dir=%s spec_len=%d", working_dir, len(spec))

    spec_truncated = len(spec) > MAX_SPEC_CHARS
    safe_spec = truncate(spec, MAX_SPEC_CHARS, "spec")

    prompt = _load_prompt(
        "execute",
        working_dir=working_dir,
        context_files=context_files or "none",
        safe_spec=safe_spec,
    )

    result = await run_gemini(prompt, working_dir=working_dir, timeout=EXECUTE_TIMEOUT)
    result = _finalize(result, _EXECUTE_KEYS, "gemini_execute")

    # Attach truncation warning on both success and error paths — an oversized
    # spec often contributes to timeouts/parse failures, and the caller needs
    # that signal to diagnose the root cause.
    if spec_truncated:
        try:
            data = json.loads(result)
            data["_warning"] = (
                f"spec was truncated from {len(spec)} to {MAX_SPEC_CHARS} chars. "
                "Gemini may have built an incomplete implementation — shorten the spec."
            )
            result = json.dumps(data)
        except json.JSONDecodeError:
            logger.warning("spec truncated but result was not JSON; warning dropped")

    return result


@mcp.tool(description=(
    "Ask Gemini to review whether Claude's architectural spec was correctly implemented "
    "by gemini_execute. Pass Claude's done conditions as done_conditions and "
    "gemini_execute's filesCreated + filesModified as changed_files. "
    "Returns verdict (PASS/FIX/BLOCKED), per-condition results, issues with fixes, and a summary."
))
async def gemini_review(
    step_title: str,
    done_conditions: str,
    changed_files: str,
    diff: str | None = None,
    execution_output: str | None = None,
    working_dir: str | None = None,
) -> str:
    """
    step_title       – Title of the step being reviewed.
    done_conditions  – Claude's original acceptance criteria (what must be true to pass).
    changed_files    – Comma-separated files created/modified (from gemini_execute output).
    diff             – Git diff output. Auto-truncated to 3500 chars.
    execution_output – gemini_execute summary + command outputs. Auto-truncated to 2000 chars.
    working_dir      – Project directory (optional; used if Gemini needs to read files).
    """
    logger.info("gemini_review step=%r", step_title)

    prompt = _load_prompt(
        "review",
        step_title=step_title,
        done_conditions=done_conditions,
        changed_files=changed_files,
        safe_diff=truncate(diff, MAX_DIFF_CHARS, "diff"),
        safe_output=truncate(execution_output, MAX_OUTPUT_CHARS, "executionOutput"),
    )

    result = await run_gemini(prompt, working_dir=working_dir, timeout=REVIEW_TIMEOUT)
    return _finalize(result, _REVIEW_KEYS, "gemini_review")


@mcp.tool(description=(
    "Ask Gemini to create an executable task plan from an objective and requirements. "
    "Returns taskName, objective, ordered steps (id/title/description), and finalDone criteria. "
    "Pass the result directly to /tp to write state.json and todo.md."
))
async def gemini_plan(
    objective: str,
    requirements: str,
    non_goals: str | None = None,
) -> str:
    """
    objective    – What the task should achieve (one sentence).
    requirements – Key constraints, tech stack, or must-haves.
    non_goals    – What is explicitly out of scope (optional).
    """
    logger.info("gemini_plan objective=%r", objective[:80])

    prompt = _load_prompt(
        "plan",
        objective=objective,
        requirements=requirements,
        non_goals=non_goals or "none",
    )

    result = await run_gemini(prompt, working_dir=None, timeout=PLAN_TIMEOUT)
    return _finalize(result, _PLAN_KEYS, "gemini_plan")


@mcp.tool(description=(
    "Verify Gemini CLI is accessible and authenticated. "
    "Returns {\"status\": \"ok\"} on success, or a structured error response. "
    "Call this before starting real work to catch auth issues early."
))
async def gemini_ping() -> str:
    """Lightweight health check — runs a minimal prompt with a short timeout."""
    logger.info("gemini_ping")
    return await run_gemini(_load_prompt("ping"), timeout=PING_TIMEOUT)
