"""Tests for gemini_mcp.core.runner — filesystem and async subprocess behaviour."""
import json
from unittest.mock import AsyncMock, patch

import pytest

import gemini_mcp.core.runner as runner_mod
from gemini_mcp.core.runner import validate_working_dir, run_gemini


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_sub(stdout="", returncode=0, stderr=""):
    """Patch _run_subprocess to return (stdout, stderr, returncode)."""
    return patch(
        "gemini_mcp.core.runner._run_subprocess",
        new=AsyncMock(return_value=(stdout, stderr, returncode)),
    )


# ── validate_working_dir ──────────────────────────────────────────────────────

class TestValidateWorkingDir:
    def test_valid_directory(self, tmp_path):
        validate_working_dir(str(tmp_path))  # must not raise

    def test_nonexistent_path_raises(self, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            validate_working_dir(str(tmp_path / "no_such_dir"))

    def test_file_not_dir_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            validate_working_dir(str(f))

    def test_allowed_root_subdir_passes(self, tmp_path, monkeypatch):
        subdir = tmp_path / "project"
        subdir.mkdir()
        monkeypatch.setattr(runner_mod, "_ALLOWED_ROOT", str(tmp_path))
        validate_working_dir(str(subdir))  # must not raise

    def test_allowed_root_itself_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_mod, "_ALLOWED_ROOT", str(tmp_path))
        validate_working_dir(str(tmp_path))  # must not raise

    def test_outside_allowed_root_raises(self, tmp_path, tmp_path_factory, monkeypatch):
        other = tmp_path_factory.mktemp("other")
        monkeypatch.setattr(runner_mod, "_ALLOWED_ROOT", str(tmp_path))
        with pytest.raises(ValueError, match="outside allowed root"):
            validate_working_dir(str(other))

    def test_no_allowed_root_skips_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_mod, "_ALLOWED_ROOT", None)
        validate_working_dir(str(tmp_path))  # must not raise even for any valid dir


# ── run_gemini ────────────────────────────────────────────────────────────────

class TestRunGemini:
    async def test_success_returns_json(self, tmp_path):
        response = '{"status": "success", "filesCreated": []}'
        with _mock_sub(stdout=response):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        assert json.loads(result) == {"status": "success", "filesCreated": []}

    async def test_working_dir_none_skips_validation(self):
        response = '{"status": "ok"}'
        with _mock_sub(stdout=response):
            result = await run_gemini("prompt", working_dir=None)
        assert json.loads(result)["status"] == "ok"

    async def test_invalid_working_dir_returns_validation_error(self):
        result = await run_gemini("prompt", working_dir="/does/not/exist/at/all")
        data = json.loads(result)
        assert data["errorType"] == "validationError"

    async def test_nonzero_exit_with_no_output_returns_gemini_error(self, tmp_path):
        with _mock_sub(returncode=1, stderr="something broke"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "geminiError"
        assert "something broke" in data["error"]

    async def test_nonzero_exit_with_stdout_still_returns_gemini_error(self, tmp_path):
        """Non-zero exit must be treated as failure even if stdout has JSON.
        Parsing a failing process's stdout can mask real errors."""
        with _mock_sub(stdout='{"status": "success"}', returncode=1, stderr="quota exceeded"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "geminiError"
        assert "quota exceeded" in data["error"]

    async def test_auth_error_unauthenticated(self, tmp_path):
        with _mock_sub(returncode=1, stderr="UNAUTHENTICATED: invalid credentials"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "authExpired"
        assert "re-authenticate" in data["error"]
        assert "stderr" in data

    async def test_auth_error_token_expired(self, tmp_path):
        with _mock_sub(returncode=1, stderr="Token has been expired or revoked"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "authExpired"

    async def test_auth_error_unauthorized(self, tmp_path):
        with _mock_sub(returncode=1, stderr="401 unauthorized"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "authExpired"

    async def test_non_auth_error_stays_gemini_error(self, tmp_path):
        with _mock_sub(returncode=1, stderr="disk quota exceeded"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "geminiError"

    async def test_parse_error_triggers_retry(self, tmp_path):
        responses = [
            ("Here is your answer in prose form.", "", 0),
            ('{"status": "ok"}', "", 0),
        ]
        mock = AsyncMock(side_effect=responses)
        with patch("gemini_mcp.core.runner._run_subprocess", mock):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        assert json.loads(result) == {"status": "ok"}

    async def test_retry_prompt_echoes_previous_failing_output(self, tmp_path):
        """On retry, the prompt must include the previous bad output so the
        model can self-correct rather than regenerate blind."""
        bad_output = "Here's the plan: step 1, step 2, step 3."
        captured_prompts = []

        async def fake_sub(cmd, use_shell, prompt, working_dir, timeout):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                return (bad_output, "", 0)
            return ('{"status": "ok"}', "", 0)

        with patch("gemini_mcp.core.runner._run_subprocess", side_effect=fake_sub):
            result = await run_gemini("do the thing", working_dir=str(tmp_path))

        assert json.loads(result) == {"status": "ok"}
        assert len(captured_prompts) == 2
        # First attempt: clean prompt, no retry suffix
        assert bad_output not in captured_prompts[0]
        assert "PREVIOUS RESPONSE" not in captured_prompts[0]
        # Second attempt: includes the failing output so Gemini can fix it
        assert bad_output in captured_prompts[1]
        assert "PREVIOUS RESPONSE" in captured_prompts[1]

    async def test_retry_prompt_truncates_large_previous_output(self, tmp_path):
        """Very long prior output is clipped so the retry prompt stays bounded."""
        huge = "x" * 5000
        captured_prompts = []

        async def fake_sub(cmd, use_shell, prompt, working_dir, timeout):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                return (huge, "", 0)
            return ('{"status": "ok"}', "", 0)

        with patch("gemini_mcp.core.runner._run_subprocess", side_effect=fake_sub):
            await run_gemini("do the thing", working_dir=str(tmp_path))

        retry_prompt = captured_prompts[1]
        assert "chars elided" in retry_prompt
        # head + tail == 1000 chars of output, plus elision marker — far less
        # than the 5000-char input.
        assert retry_prompt.count("x") < 1500

    async def test_retry_prompt_shows_tail_of_large_output(self, tmp_path):
        """The tail of the previous output must be visible on retry — JSON
        parse errors typically live near the end, so head-only truncation
        would hide them."""
        # Distinctive head and tail so we can tell which part survives.
        head   = "HEAD_MARKER_" + "a" * 400
        middle = "m" * 3000
        tail   = "b" * 400 + "_TAIL_MARKER"
        bad    = head + middle + tail
        captured_prompts = []

        async def fake_sub(cmd, use_shell, prompt, working_dir, timeout):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                return (bad, "", 0)
            return ('{"status": "ok"}', "", 0)

        with patch("gemini_mcp.core.runner._run_subprocess", side_effect=fake_sub):
            await run_gemini("do the thing", working_dir=str(tmp_path))

        retry_prompt = captured_prompts[1]
        assert "HEAD_MARKER_" in retry_prompt
        assert "_TAIL_MARKER" in retry_prompt
        assert "chars elided" in retry_prompt

    async def test_retry_prompt_surfaces_parse_error_position(self, tmp_path):
        """When the previous output was structurally invalid JSON, the retry
        prompt must include the parse error message so Gemini can target the
        fix instead of regenerating blind."""
        # Trailing comma inside the object — json.loads raises JSONDecodeError
        # with a position pointer, which we want to surface to Gemini.
        bad_json = '{"status": "ok", "items": [1, 2, 3,]}'
        captured_prompts = []

        async def fake_sub(cmd, use_shell, prompt, working_dir, timeout):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                return (bad_json, "", 0)
            return ('{"status": "ok"}', "", 0)

        with patch("gemini_mcp.core.runner._run_subprocess", side_effect=fake_sub):
            result = await run_gemini("do the thing", working_dir=str(tmp_path))

        # The retry must succeed because attempt-2 returns clean JSON.
        assert json.loads(result) == {"status": "ok"}
        retry_prompt = captured_prompts[1]
        assert "Parse failure:" in retry_prompt
        # JSONDecodeError message should mention line/column/char position.
        assert "line" in retry_prompt and "column" in retry_prompt

    async def test_parse_error_response_includes_position(self, tmp_path):
        """The final parseError response exposes the position info so the
        caller (Claude) can diagnose without re-running the tool."""
        with _mock_sub(stdout="not json at all"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "parseError"
        assert "parseError" in data  # field carrying the diagnostic

    async def test_parse_error_after_retry_returns_error(self, tmp_path):
        with _mock_sub(stdout="not json"):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "parseError"
        assert "rawOutput" in data
        assert data["rawOutput"] == "not json"

    async def test_timeout_returns_timeout_error(self, tmp_path):
        import asyncio
        with patch(
            "gemini_mcp.core.runner._run_subprocess",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ):
            result = await run_gemini("prompt", working_dir=str(tmp_path), timeout=30)
        data = json.loads(result)
        assert data["errorType"] == "timeout"
        assert "30s" in data["error"]

    async def test_unexpected_exception_returns_run_error(self, tmp_path):
        with patch(
            "gemini_mcp.core.runner._run_subprocess",
            new=AsyncMock(side_effect=OSError("disk full")),
        ):
            result = await run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "runError"
        assert "disk full" in data["error"]

    async def test_model_env_var_included_in_cmd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", "gemini-2.5-pro")
        captured = {}
        async def fake_sub(cmd, use_shell, prompt, working_dir, timeout):
            captured["cmd"] = cmd
            return ('{"ok": true}', "", 0)
        with patch("gemini_mcp.core.runner._run_subprocess", side_effect=fake_sub):
            await run_gemini("prompt", working_dir=str(tmp_path))
        cmd = captured["cmd"]
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        assert "gemini-2.5-pro" in cmd_str

    async def test_prompt_with_non_ascii_does_not_crash(self, tmp_path):
        with _mock_sub(stdout='{"ok": true}'):
            result = await run_gemini("émojis: 🎉 漢字 Ünïcödé", working_dir=str(tmp_path))
        assert json.loads(result) == {"ok": True}


# ── _timeout_env ──────────────────────────────────────────────────────────────

class TestTimeoutEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("GEMINI_MCP_FOO_TIMEOUT", raising=False)
        assert runner_mod._timeout_env("GEMINI_MCP_FOO_TIMEOUT", 123.0) == 123.0

    def test_integer_value_parsed(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MCP_FOO_TIMEOUT", "45")
        assert runner_mod._timeout_env("GEMINI_MCP_FOO_TIMEOUT", 0.0) == 45.0

    def test_float_value_parsed(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MCP_FOO_TIMEOUT", "30.5")
        assert runner_mod._timeout_env("GEMINI_MCP_FOO_TIMEOUT", 0.0) == 30.5

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MCP_FOO_TIMEOUT", "not-a-number")
        assert runner_mod._timeout_env("GEMINI_MCP_FOO_TIMEOUT", 99.0) == 99.0


# ── _validated_model ──────────────────────────────────────────────────────────

class TestValidatedModel:
    def test_valid_model_name_accepted(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", "gemini-2.5-pro")
        assert runner_mod._validated_model() == "gemini-2.5-pro"

    def test_model_with_dots_accepted(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", "gemini-1.5-flash-8b")
        assert runner_mod._validated_model() == "gemini-1.5-flash-8b"

    def test_none_returns_none(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", None)
        assert runner_mod._validated_model() is None

    def test_shell_injection_rejected(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", 'foo" && del C:\\*')
        assert runner_mod._validated_model() is None

    def test_semicolon_rejected(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", "model;evil")
        assert runner_mod._validated_model() is None

    def test_space_rejected(self, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", "model name")
        assert runner_mod._validated_model() is None


# ── _make_cmd ─────────────────────────────────────────────────────────────────

class TestMakeCmd:
    def test_windows_returns_shell_string(self, monkeypatch):
        monkeypatch.setattr(runner_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(runner_mod, "_MODEL", None)
        cmd, use_shell = runner_mod._make_cmd()
        assert use_shell is True
        assert isinstance(cmd, str)
        assert "-p" in cmd and "-o text" in cmd and "-y" in cmd

    def test_windows_includes_model(self, monkeypatch):
        monkeypatch.setattr(runner_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(runner_mod, "_MODEL", "gemini-2.5-pro")
        cmd, use_shell = runner_mod._make_cmd()
        assert use_shell is True
        assert "gemini-2.5-pro" in cmd

    def test_non_windows_returns_list(self, monkeypatch):
        monkeypatch.setattr(runner_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(runner_mod, "_MODEL", None)
        cmd, use_shell = runner_mod._make_cmd()
        assert use_shell is False
        assert isinstance(cmd, list)
        assert "-y" in cmd

    def test_non_windows_includes_model_as_list_args(self, monkeypatch):
        monkeypatch.setattr(runner_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(runner_mod, "_MODEL", "gemini-2.5-pro")
        cmd, use_shell = runner_mod._make_cmd()
        assert use_shell is False
        assert "-m" in cmd and "gemini-2.5-pro" in cmd

    def test_invalid_model_excluded_from_cmd(self, monkeypatch):
        monkeypatch.setattr(runner_mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(runner_mod, "_MODEL", "bad;model")
        cmd, _ = runner_mod._make_cmd()
        assert "-m" not in cmd


# ── _run_subprocess ───────────────────────────────────────────────────────────

class TestRunSubprocess:
    async def test_returns_stdout_stderr_returncode(self, tmp_path):
        """_run_subprocess decodes bytes and returns (stdout, stderr, returncode)."""
        import asyncio

        class _FakeProc:
            returncode = 0
            async def communicate(self, input=None):
                return b'{"status": "ok"}', b""
            def kill(self): pass

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_FakeProc())):
            stdout, stderr, rc = await runner_mod._run_subprocess(
                ["gemini", "-p", " "], False, "prompt", str(tmp_path), 30
            )
        assert stdout == '{"status": "ok"}'
        assert stderr == ""
        assert rc == 0

    async def test_kills_process_on_timeout(self, tmp_path):
        """Process is killed when asyncio.wait_for raises TimeoutError."""
        import asyncio

        killed = []

        class _SlowProc:
            returncode = None
            async def communicate(self, input=None):  # pragma: no cover
                await asyncio.sleep(999)
            def kill(self):
                killed.append(True)

        async def fake_wait_for(coro, timeout):
            raise asyncio.TimeoutError

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_SlowProc())):
            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                with pytest.raises(asyncio.TimeoutError):
                    await runner_mod._run_subprocess(
                        ["gemini", "-p", " "], False, "prompt", str(tmp_path), 1
                    )
        assert killed, "process.kill() was not called on timeout"

    async def test_shell_true_uses_create_subprocess_shell(self, tmp_path):
        """Windows path: use_shell=True calls create_subprocess_shell, not exec."""
        import asyncio

        class _FakeProc:
            returncode = 0
            async def communicate(self, input=None):
                return b'{"ok": true}', b""
            def kill(self): pass

        shell_mock = AsyncMock(return_value=_FakeProc())
        exec_mock  = AsyncMock(return_value=_FakeProc())

        with patch("asyncio.create_subprocess_shell", shell_mock):
            with patch("asyncio.create_subprocess_exec", exec_mock):
                await runner_mod._run_subprocess(
                    '"gemini.cmd" -p " " -y', True, "prompt", str(tmp_path), 30
                )
        shell_mock.assert_called_once()
        exec_mock.assert_not_called()
