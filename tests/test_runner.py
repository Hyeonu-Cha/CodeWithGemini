"""Tests for gemini_mcp.core.runner — filesystem and subprocess behaviour."""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import gemini_mcp.core.runner as runner_mod
from gemini_mcp.core.runner import validate_working_dir, run_gemini


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_proc(stdout="", returncode=0, stderr=""):
    m = MagicMock()
    m.stdout    = stdout
    m.returncode = returncode
    m.stderr    = stderr
    return m


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
    def test_success_returns_json(self, tmp_path):
        response = '{"status": "success", "filesCreated": []}'
        with patch("subprocess.run", return_value=_mock_proc(stdout=response)):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        assert json.loads(result) == {"status": "success", "filesCreated": []}

    def test_working_dir_none_skips_validation(self):
        response = '{"status": "ok"}'
        with patch("subprocess.run", return_value=_mock_proc(stdout=response)):
            result = run_gemini("prompt", working_dir=None)
        assert json.loads(result)["status"] == "ok"

    def test_invalid_working_dir_returns_validation_error(self):
        result = run_gemini("prompt", working_dir="/does/not/exist/at/all")
        data = json.loads(result)
        assert data["errorType"] == "validationError"

    def test_nonzero_exit_with_no_output_returns_gemini_error(self, tmp_path):
        with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="something broke")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "geminiError"
        assert "something broke" in data["error"]

    def test_auth_error_unauthenticated(self, tmp_path):
        with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="UNAUTHENTICATED: invalid credentials")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "authExpired"
        assert "re-authenticate" in data["error"]
        assert "stderr" in data

    def test_auth_error_token_expired(self, tmp_path):
        with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="Token has been expired or revoked")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "authExpired"

    def test_auth_error_unauthorized(self, tmp_path):
        with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="401 unauthorized")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "authExpired"

    def test_non_auth_error_stays_gemini_error(self, tmp_path):
        with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="disk quota exceeded")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "geminiError"

    def test_parse_error_triggers_retry(self, tmp_path):
        # First attempt: prose. Second attempt: valid JSON.
        responses = [
            _mock_proc(stdout="Here is your answer in prose form."),
            _mock_proc(stdout='{"status": "ok"}'),
        ]
        with patch("subprocess.run", side_effect=responses):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        assert json.loads(result) == {"status": "ok"}

    def test_parse_error_after_retry_returns_error(self, tmp_path):
        # Both attempts return non-JSON
        with patch("subprocess.run", return_value=_mock_proc(stdout="not json")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "parseError"
        assert "rawOutput" in data
        assert data["rawOutput"] == "not json"

    def test_timeout_returns_timeout_error(self, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            result = run_gemini("prompt", working_dir=str(tmp_path), timeout=30)
        data = json.loads(result)
        assert data["errorType"] == "timeout"
        assert "30s" in data["error"]
        assert "attempt" in data["error"]

    def test_unexpected_exception_returns_run_error(self, tmp_path):
        with patch("subprocess.run", side_effect=OSError("disk full")):
            result = run_gemini("prompt", working_dir=str(tmp_path))
        data = json.loads(result)
        assert data["errorType"] == "runError"
        assert "disk full" in data["error"]

    def test_model_env_var_included_in_cmd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner_mod, "_MODEL", "gemini-2.5-pro")
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _mock_proc(stdout='{"ok": true}')
        with patch("subprocess.run", side_effect=fake_run):
            run_gemini("prompt", working_dir=str(tmp_path))
        cmd = captured["cmd"]
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        assert "gemini-2.5-pro" in cmd_str

    def test_subprocess_uses_utf8_encoding(self, tmp_path):
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["kwargs"] = kwargs
            return _mock_proc(stdout='{"ok": true}')
        with patch("subprocess.run", side_effect=fake_run):
            run_gemini("prompt with emoji 🎉", working_dir=str(tmp_path))
        assert captured["kwargs"].get("encoding") == "utf-8"

    def test_prompt_with_non_ascii_does_not_crash(self, tmp_path):
        # Verifies encoding="utf-8" is passed so non-ASCII chars don't raise UnicodeEncodeError
        with patch("subprocess.run", return_value=_mock_proc(stdout='{"ok": true}')):
            result = run_gemini("émojis: 🎉 漢字 Ünïcödé", working_dir=str(tmp_path))
        assert json.loads(result) == {"ok": True}


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
