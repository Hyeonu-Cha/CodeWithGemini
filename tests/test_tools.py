"""Tests for gemini_mcp.tools — contracts, warnings, and schema validation."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from gemini_mcp.core.parsers import MAX_SPEC_CHARS
from gemini_mcp.tools import gemini_execute, gemini_review, gemini_ping, gemini_plan, _inject_schema_warning, _load_prompt


# ── helpers ───────────────────────────────────────────────────────────────────

def _patch_run(response: dict):
    """Patch run_gemini with an AsyncMock returning the given dict as JSON."""
    return patch(
        "gemini_mcp.tools.run_gemini",
        new=AsyncMock(return_value=json.dumps(response)),
    )


# ── _load_prompt ──────────────────────────────────────────────────────────────

class TestLoadPrompt:
    def test_execute_template_fills_variables(self):
        prompt = _load_prompt("execute", working_dir="/tmp/proj", context_files="none", safe_spec="build X")
        assert "/tmp/proj" in prompt
        assert "build X" in prompt
        assert "<<<" not in prompt  # no unfilled placeholders

    def test_review_template_fills_variables(self):
        prompt = _load_prompt("review", step_title="S1", done_conditions="tests pass",
                              changed_files="foo.py", safe_diff="diff content", safe_output="output")
        assert "S1" in prompt
        assert "tests pass" in prompt
        assert "diff content" in prompt
        assert "<<<" not in prompt

    def test_ping_template_loads(self):
        prompt = _load_prompt("ping")
        assert "ok" in prompt
        assert "<<<" not in prompt

    def test_json_braces_preserved_in_execute(self):
        prompt = _load_prompt("execute", working_dir="/tmp", context_files="none", safe_spec="x")
        assert '"status"' in prompt
        assert '"filesCreated"' in prompt

    def test_unfilled_placeholder_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="gemini_mcp.tools"):
            _load_prompt("execute", working_dir="/tmp", context_files="none")
        assert any("unfilled placeholders" in r.message for r in caplog.records)

    def test_no_warning_when_all_placeholders_filled(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="gemini_mcp.tools"):
            _load_prompt("execute", working_dir="/tmp", context_files="none", safe_spec="x")
        assert not any("unfilled placeholders" in r.message for r in caplog.records)

    def test_single_pass_prevents_double_replacement(self):
        prompt = _load_prompt(
            "execute",
            working_dir="/tmp",
            context_files="none",
            safe_spec="use <<<context_files>>> as reference",
        )
        assert "use <<<context_files>>> as reference" in prompt


# ── _inject_schema_warning ────────────────────────────────────────────────────

class TestInjectSchemaWarning:
    def test_no_warning_when_all_keys_present(self):
        data = {"a": 1, "b": 2}
        _inject_schema_warning(data, {"a", "b"}, "tool")
        assert "_schemaWarning" not in data

    def test_warning_injected_for_missing_keys(self):
        data = {"a": 1}
        _inject_schema_warning(data, {"a", "b", "c"}, "tool")
        assert "_schemaWarning" in data
        assert "b" in data["_schemaWarning"] or "c" in data["_schemaWarning"]

    def test_warning_contains_tool_name(self):
        data = {}
        _inject_schema_warning(data, {"x"}, "gemini_execute")
        assert "gemini_execute" in data["_schemaWarning"]


# ── gemini_execute ────────────────────────────────────────────────────────────

class TestGeminiExecute:
    _FULL_RESPONSE = {
        "status": "success",
        "filesCreated": ["foo.py"],
        "filesModified": [],
        "commandsRun": [],
        "summary": "Done.",
        "issues": [],
    }

    async def test_clean_response_no_warnings(self):
        with _patch_run(self._FULL_RESPONSE):
            result = await gemini_execute(spec="build X", working_dir="/tmp")
        data = json.loads(result)
        assert "_schemaWarning" not in data
        assert "_warning" not in data

    async def test_spec_truncation_injects_warning(self):
        long_spec = "x" * (MAX_SPEC_CHARS + 1)
        with _patch_run(self._FULL_RESPONSE):
            result = await gemini_execute(spec=long_spec, working_dir="/tmp")
        data = json.loads(result)
        assert "_warning" in data
        assert "truncated" in data["_warning"]

    async def test_no_truncation_warning_for_short_spec(self):
        with _patch_run(self._FULL_RESPONSE):
            result = await gemini_execute(spec="short spec", working_dir="/tmp")
        data = json.loads(result)
        assert "_warning" not in data

    async def test_schema_warning_on_missing_keys(self):
        with _patch_run({"status": "success"}):
            result = await gemini_execute(spec="build X", working_dir="/tmp")
        data = json.loads(result)
        assert "_schemaWarning" in data

    async def test_error_response_passes_through_without_schema_check(self):
        error = {"errorType": "timeout", "error": "timed out"}
        with _patch_run(error):
            result = await gemini_execute(spec="build X", working_dir="/tmp")
        data = json.loads(result)
        assert data["errorType"] == "timeout"
        assert "_schemaWarning" not in data

    async def test_both_truncation_and_schema_warning_coexist(self):
        long_spec = "x" * (MAX_SPEC_CHARS + 1)
        with _patch_run({"status": "partial"}):
            result = await gemini_execute(spec=long_spec, working_dir="/tmp")
        data = json.loads(result)
        assert "_warning" in data
        assert "_schemaWarning" in data


# ── gemini_review ─────────────────────────────────────────────────────────────

class TestGeminiReview:
    _FULL_RESPONSE = {
        "verdict": "PASS",
        "doneConditionsMet": [{"condition": "tests pass", "met": True, "evidence": "CI green"}],
        "issues": [],
        "summary": "All good.",
    }

    async def test_clean_response_no_schema_warning(self):
        with _patch_run(self._FULL_RESPONSE):
            result = await gemini_review(
                step_title="S1", done_conditions="tests pass", changed_files="foo.py"
            )
        data = json.loads(result)
        assert "_schemaWarning" not in data

    async def test_schema_warning_on_missing_keys(self):
        with _patch_run({"verdict": "PASS"}):
            result = await gemini_review(
                step_title="S1", done_conditions="tests pass", changed_files="foo.py"
            )
        data = json.loads(result)
        assert "_schemaWarning" in data

    async def test_error_response_passes_through(self):
        error = {"errorType": "parseError", "error": "bad json", "rawOutput": "..."}
        with _patch_run(error):
            result = await gemini_review(
                step_title="S1", done_conditions="ok", changed_files="foo.py"
            )
        data = json.loads(result)
        assert data["errorType"] == "parseError"
        assert "_schemaWarning" not in data


# ── gemini_ping ───────────────────────────────────────────────────────────────

class TestGeminiPing:
    async def test_success(self):
        with patch("gemini_mcp.tools.run_gemini", new=AsyncMock(return_value='{"status": "ok"}')):
            result = await gemini_ping()
        assert json.loads(result) == {"status": "ok"}

    async def test_error_passthrough(self):
        error = {"errorType": "geminiError", "error": "not authenticated"}
        with patch("gemini_mcp.tools.run_gemini", new=AsyncMock(return_value=json.dumps(error))):
            result = await gemini_ping()
        assert json.loads(result)["errorType"] == "geminiError"


# ── gemini_plan ───────────────────────────────────────────────────────────────

class TestGeminiPlan:
    _FULL_RESPONSE = {
        "taskName": "Build auth module",
        "objective": {
            "goal": "Add JWT auth",
            "nonGoals": "OAuth",
            "doneWhen": "Users can log in with JWT",
        },
        "steps": [
            {"id": "S1", "title": "Create auth module", "description": "Add jwt logic"}
        ],
        "finalDone": ["Users can log in", "Tests pass"],
    }

    async def test_clean_response_no_warnings(self):
        with _patch_run(self._FULL_RESPONSE):
            result = await gemini_plan(objective="Add auth", requirements="JWT, Python")
        data = json.loads(result)
        assert "_schemaWarning" not in data
        assert data["taskName"] == "Build auth module"

    async def test_schema_warning_on_missing_keys(self):
        with _patch_run({"taskName": "x"}):
            result = await gemini_plan(objective="Add auth", requirements="JWT")
        data = json.loads(result)
        assert "_schemaWarning" in data

    async def test_non_goals_defaults_to_none(self):
        with _patch_run(self._FULL_RESPONSE):
            result = await gemini_plan(objective="Add auth", requirements="JWT")
        data = json.loads(result)
        assert "taskName" in data

    async def test_error_passthrough(self):
        error = {"errorType": "timeout", "error": "timed out"}
        with _patch_run(error):
            result = await gemini_plan(objective="Add auth", requirements="JWT")
        data = json.loads(result)
        assert data["errorType"] == "timeout"
        assert "_schemaWarning" not in data

    def test_plan_template_loads(self):
        prompt = _load_prompt("plan", objective="Build X", requirements="Python", non_goals="none")
        assert "Build X" in prompt
        assert "Python" in prompt
        assert "<<<" not in prompt
