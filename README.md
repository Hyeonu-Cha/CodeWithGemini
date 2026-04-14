# CodeBridgeMCP

An MCP server that bridges Claude and Gemini CLI, reducing Claude's output token usage by ~84% by delegating file creation and code review to Gemini.

**Architecture: Claude = Architect, Gemini = Builder**

- Claude designs specs, makes decisions, orchestrates the workflow
- Gemini receives the spec and autonomously creates/modifies files via its built-in file tools
- Claude never writes the code itself — it only calls MCP tools

---

## How It Works

```
Claude Code
    │
    │  gemini_execute(spec, working_dir)
    │  gemini_review(conditions, diff, ...)
    ▼
gemini_mcp (MCP Server)
    │
    │  gemini -p " " -o text -y   (prompt via stdin pipe)
    ▼
Gemini CLI (subprocess)
    └── reads/writes files autonomously in working_dir
```

Claude generates ~50 tokens (a tool call). Gemini generates the code. Without this, Claude would generate ~2300–3800 output tokens per step writing the code itself.

---

## Prerequisites

- **Python 3.10+**
- **Gemini CLI**: `npm install -g @google/gemini-cli`
- **Gemini CLI authenticated** (run `gemini` once interactively to log in)

---

## Installation

**Clone and install:**
```bash
git clone https://github.com/Hyeonu-Cha/CodeWithGemini.git
cd CodeWithGemini
pip install -e .
```

**Generate `.mcp.json` for your local environment:**
```bat
setup.bat
```

This writes `.mcp.json` with the full paths to your active `python` and `gemini` binaries. Run it once after cloning, or again after upgrading Python.

---

## Project Structure

```
CodeWithGemini/
├── gemini_mcp/
│   ├── __init__.py        # FastMCP instance
│   ├── __main__.py        # Entry point (python -m gemini_mcp)
│   ├── tools.py           # MCP tool definitions
│   └── core/
│       ├── parsers.py     # truncate(), extract_json(), size constants
│       └── runner.py      # run_gemini(), validate_working_dir()
├── server.py              # Legacy shim (python server.py) — not used by .mcp.json
├── setup_mcp.py           # Script that generates .mcp.json from active environment
├── setup.bat              # Windows wrapper: calls python setup_mcp.py
├── .mcp.json              # MCP server registration (auto-generated, do not hand-edit)
├── pyproject.toml         # Package metadata, requires-python>=3.10
└── requirements.txt       # mcp>=1.27.0,<2.0
```

---

## MCP Tools

### `gemini_execute`

Sends Claude's architectural spec to Gemini. Gemini uses its file tools to create and modify files autonomously.

| Parameter | Type | Description |
|-----------|------|-------------|
| `spec` | `str` | Claude's complete spec — what to build and how |
| `working_dir` | `str` | Absolute path to the project directory |
| `context_files` | `str?` | Comma-separated files Gemini should read first |

**Returns:**
```json
{
  "status": "success|partial|failed",
  "filesCreated": ["path/to/file"],
  "filesModified": ["path/to/file"],
  "commandsRun": [{"command": "...", "output": "...", "exitCode": 0}],
  "summary": "What was built",
  "issues": []
}
```

If your spec exceeds 8000 chars, a `_warning` field is injected into the response so Claude knows to split the task.

---

### `gemini_review`

Asks Gemini to verify whether the spec was correctly implemented. Pass the diff from `git diff HEAD` and the files from `gemini_execute`'s response.

| Parameter | Type | Description |
|-----------|------|-------------|
| `step_title` | `str` | Title of the step being reviewed |
| `done_conditions` | `str` | Acceptance criteria |
| `changed_files` | `str` | Comma-separated files from `gemini_execute` output |
| `diff` | `str?` | Git diff output (auto-truncated to 3500 chars) |
| `execution_output` | `str?` | `gemini_execute` summary (auto-truncated to 2000 chars) |
| `working_dir` | `str?` | Project directory (if Gemini needs to read files) |

**Returns:**
```json
{
  "verdict": "PASS|FIX|BLOCKED",
  "doneConditionsMet": [{"condition": "...", "met": true, "evidence": "..."}],
  "issues": [{"severity": "high|medium|low", "description": "...", "fix": "..."}],
  "summary": "Review summary"
}
```

---

## Typical Workflow (inside `/tr`)

```
1. Claude reads state.json  →  knows what step to build
2. Claude writes spec       →  pure reasoning, no file I/O
3. gemini_execute(spec)     →  Gemini builds the files
4. git diff HEAD            →  Claude captures the diff
5. gemini_review(diff, ...) →  Gemini reviews its own work
6. Claude reads verdict     →  PASS → advance step, FIX → retry, BLOCKED → escalate
7. Claude writes state.json →  done
```

---

## Error Types

All tools return structured JSON on failure:

| `errorType` | Meaning |
|-------------|---------|
| `validationError` | `working_dir` does not exist, is not a directory, or is outside `GEMINI_MCP_ALLOWED_ROOT` |
| `geminiError` | Gemini exited non-zero with no output (check `error` field for stderr) |
| `parseError` | Gemini did not return valid JSON after one retry (`rawOutput` shows first 500 chars) |
| `timeout` | Gemini did not respond within the timeout (execute: 300s, review: 120s) |
| `runError` | Unexpected exception in the server process |

---

## Security

**Path restriction (optional)**

By default `working_dir` is validated to exist and be a directory. To also restrict it to a subtree, set the `GEMINI_MCP_ALLOWED_ROOT` environment variable:

```bat
set GEMINI_MCP_ALLOWED_ROOT=C:\Users\gotow\projects
```

Any `working_dir` outside that root will be rejected with a `validationError` before Gemini is invoked.

**Prompt design**

Gemini runs with `-y` (auto-approve file tools). This is intentional — it allows autonomous file creation — but means the `spec` parameter should come from trusted sources (i.e. Claude) only.

---

## Token Savings

| Workflow | Before | After | Reduction |
|----------|--------|-------|-----------|
| Per `/tr` step | ~2300–3800 output tokens | ~540 | **~85%** |
| Per `/tp` plan | ~3000–6000 output tokens | ~450 | **~87%** |
| Per `/review` | ~800–1200 output tokens | ~180 | **~83%** |

Output tokens cost ~10x more than input tokens. Gemini's responses are read back by Claude as input tokens (cheap).

---

## Troubleshooting

**MCP server won't start**
- Run `setup.bat` to regenerate `.mcp.json` with correct local paths
- Verify `pip install -e .` was run in the Python that `.mcp.json` points to
- Verify `gemini` is on PATH: `where gemini`

**`errorType: validationError`**
- `working_dir` path doesn't exist or is outside `GEMINI_MCP_ALLOWED_ROOT`
- Check the `error` field for the exact reason

**`errorType: geminiError`**
- Usually means Gemini CLI needs re-authentication — run `gemini` interactively once

**`errorType: parseError`**
- Gemini returned prose instead of JSON even after an automatic retry
- Check `rawOutput` to see what Gemini actually said — often an auth prompt or quota message
