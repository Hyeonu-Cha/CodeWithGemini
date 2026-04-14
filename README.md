# CodeBridgeMCP

An MCP server that bridges Claude and Gemini CLI, reducing Claude's output token usage by ~84% by delegating planning, file creation, and code review to Gemini.

**Architecture: Claude = Architect, Gemini = Builder**

- Claude designs specs, makes decisions, orchestrates the workflow
- Gemini plans tasks, builds files, and reviews its own work via MCP tools
- Claude never writes the code itself — it only calls MCP tools and writes state files

---

## How It Works

```
Claude Code
    │
    │  gemini_plan(objective, requirements)
    │  gemini_execute(spec, working_dir)
    │  gemini_review(conditions, diff, ...)
    │  gemini_ping()
    ▼
gemini_mcp (MCP Server)
    │
    │  gemini -p " " -o text -y   (prompt via stdin pipe)
    ▼
Gemini CLI (subprocess)
    └── reads/writes files autonomously in working_dir
```

Claude generates ~50 tokens (a tool call). Gemini generates the plan or code. Without this, Claude would generate ~2300–3800 output tokens per step writing the code itself.

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
setup.bat       # Windows
./setup.sh      # Linux / macOS
```

This writes `.mcp.json` with the full paths to your active `python` and `gemini` binaries. Run it once after cloning, or again after upgrading Python.

---

## Project Structure

```
CodeWithGemini/
├── gemini_mcp/
│   ├── __init__.py        # FastMCP instance ("gemini-builder")
│   ├── __main__.py        # Entry point: python -m gemini_mcp
│   ├── tools.py           # MCP tool definitions
│   ├── core/
│   │   ├── parsers.py     # truncate(), extract_json(), size constants
│   │   └── runner.py      # run_gemini(), validate_working_dir(), timeouts
│   └── prompts/
│       ├── plan.txt       # gemini_plan prompt template
│       ├── execute.txt    # gemini_execute prompt template
│       ├── review.txt     # gemini_review prompt template
│       └── ping.txt       # gemini_ping prompt template
├── tests/
│   ├── test_tools.py      # Tool contract tests (82 total)
│   ├── test_runner.py     # Runner/subprocess tests
│   └── test_parsers.py    # Parser/truncation tests
├── setup_mcp.py           # Generates .mcp.json from active environment
├── setup.bat              # Windows wrapper
├── setup.sh               # Linux/macOS wrapper
├── .mcp.json.example      # Template for .mcp.json (committed)
└── pyproject.toml         # Package metadata, requires-python>=3.10
```

---

## MCP Tools

### `gemini_plan`

Asks Gemini to create an executable task plan from an objective and requirements. Use this at the start of a task before calling `gemini_execute`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `objective` | `str` | One sentence goal |
| `requirements` | `str` | Key constraints, tech stack, must-haves |
| `non_goals` | `str?` | What is explicitly out of scope |

**Returns:**
```json
{
  "taskName": "short descriptive name",
  "objective": {
    "goal": "one sentence goal",
    "nonGoals": "out of scope",
    "doneWhen": "acceptance summary"
  },
  "steps": [
    { "id": "S1", "title": "...", "description": "..." }
  ],
  "finalDone": ["criterion 1", "criterion 2"]
}
```

---

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

If your spec exceeds 8000 chars, a `_warning` field is injected so Claude knows to shorten it.

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

### `gemini_ping`

Verifies Gemini CLI is accessible and authenticated. Call this before starting real work to catch auth issues early.

**Returns:** `{"status": "ok"}` on success, or a structured error response.

---

## Typical Workflow (inside `/tr`)

```
1. Claude reads state.json      →  knows what step to build
2. Claude writes spec           →  pure reasoning, no file I/O
3. gemini_execute(spec)         →  Gemini builds the files
4. git diff HEAD                →  Claude captures the diff
5. gemini_review(diff, ...)     →  Gemini reviews its own work
6. Claude reads verdict         →  PASS → finalize, FIX → retry with fixes, BLOCKED → escalate
7. Claude spot-checks           →  only for subjective steps (tests, docs, config)
8. Claude writes state.json     →  done
```

---

## Review Logic

`gemini_review` returns one of three verdicts:

| Verdict | Action |
|---------|--------|
| `PASS` | Finalize the step |
| `FIX` | Claude extracts `issues[]`, revises spec, calls `gemini_execute` again (max 1 retry) |
| `BLOCKED` | Step marked blocked, escalated to user |

**Subjective/Meta steps** (writing tests, docs, CI/CD config, mocks) also trigger a Claude spot-check after `gemini_review` PASS — since there is no objective output to verify. Claude reads the changed files and checks for circular, shallow, or incorrect implementations.

---

## Error Types

All tools return structured JSON on failure:

| `errorType` | Meaning |
|-------------|---------|
| `validationError` | `working_dir` does not exist, is not a directory, or is outside `GEMINI_MCP_ALLOWED_ROOT` |
| `authExpired` | Gemini returned an auth error — re-authenticate by running `gemini` interactively |
| `geminiError` | Gemini exited non-zero (check `error` and `stderr` fields) |
| `parseError` | Gemini did not return valid JSON after one retry (`rawOutput` shows what it said) |
| `timeout` | Gemini did not respond within the timeout (plan: 120s, execute: 300s, review: 120s) |
| `runError` | Unexpected exception in the server process |

---

## Security

**Path restriction (optional)**

By default `working_dir` is validated to exist and be a directory. To also restrict it to a subtree:

```bat
set GEMINI_MCP_ALLOWED_ROOT=C:\Users\yourname\projects
```

Any `working_dir` outside that root will be rejected with a `validationError` before Gemini is invoked.

**Prompt-level boundary**

The `execute.txt` prompt explicitly forbids Gemini from reading or writing files outside `working_dir`, providing a defence-in-depth layer on top of `GEMINI_MCP_ALLOWED_ROOT`.

**Model override**

Override the Gemini model via environment variable:
```bat
set GEMINI_MCP_MODEL=gemini-2.5-pro
```

Only alphanumeric, `.`, `-`, `_` characters are accepted — any other value is silently ignored to prevent shell injection.

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
- Run `setup.bat` / `setup.sh` to regenerate `.mcp.json` with correct local paths
- Verify `pip install -e .` was run in the Python that `.mcp.json` points to
- Verify `gemini` is on PATH: `where gemini` (Windows) / `which gemini` (Linux/macOS)

**`errorType: authExpired`**
- Run `gemini` interactively once to refresh credentials

**`errorType: validationError`**
- `working_dir` path doesn't exist or is outside `GEMINI_MCP_ALLOWED_ROOT`
- Check the `error` field for the exact reason

**`errorType: geminiError`**
- Check the `stderr` field — usually an auth or quota issue

**`errorType: parseError`**
- Gemini returned prose instead of JSON even after an automatic retry
- Check `rawOutput` — often an auth prompt or quota message
