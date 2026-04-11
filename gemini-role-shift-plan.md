# Gemini Role Shift Plan: Claude + Gemini via MCP Server

**Date**: 2026-04-11 (updated)
**Objective**: Offload all generative work (design, build specs, review) from Claude to Gemini via an MCP server. Claude becomes orchestrator + file executor only. No Codex. WezTerm preserved for manual use.

---

## 1. Decision Summary

| Decision | Choice | Reason |
|----------|--------|--------|
| Provider setup | Claude + Gemini only | Codex not in active use |
| Integration method | MCP Server | Structured tool calls, no fragile text parsing, sync |
| Gemini invocation | CLI subprocess (`-p -o json -y`) | No separate API key, same auth as existing session |
| File operations | Claude directly (built-in tools) | Claude Code already has Read/Write/Edit/Bash |
| WezTerm | Kept for manual queries | Gemini pane stays but not used for MCP calls |

---

## 2. Current State: Where Claude Burns Tokens

### Token cost per `/tr` step today (output tokens — expensive)

| Step | Claude Activity | Est. Output Tokens |
|------|-----------------|--------------------|
| 2.1  | Independent step design (full spec) | ~600–1000 |
| 2.3  | Merge two designs, resolve conflicts | ~400–700 |
| 3    | Split check decision | ~200–400 |
| 4    | Build FileOpsREQ JSON | ~400–600 |
| 8    | Initial code review + assessment | ~500–800 |
| 8.5  | Test analysis + final PASS/FIX | ~200–300 |
| **Total** | | **~2300–3800 output tokens/step** |

Planning (`/tp`): ~3000–6000 output tokens per task.

**Root cause**: Claude generates all the content — specs, JSON, reviews. Output tokens cost ~10x more than input tokens.

---

## 3. New Architecture

```
┌─────────────────────────────────────────────────────┐
│  WezTerm                                             │
│                                                      │
│  ┌──────────────────────────────┐                   │
│  │  Claude Code (pane 0)        │                   │
│  │  • Orchestrates flow         │                   │
│  │  • Reads/writes files        │◄──── MCP tools   │
│  │  │  (Write, Edit, Bash)      │                   │
│  │  • Makes final decisions     │                   │
│  └──────────────────────────────┘                   │
│                                                      │
│  ┌──────────────────────────────┐                   │
│  │  Gemini pane (pane 1)        │                   │
│  │  Manual queries only         │                   │
│  │  (not used for MCP calls)    │                   │
│  └──────────────────────────────┘                   │
└─────────────────────────────────────────────────────┘
                     │
                     │ MCP tool calls (structured JSON)
                     ▼
┌─────────────────────────────────────────────────────┐
│  Gemini MCP Server (background process)              │
│                                                      │
│  Tools:                                              │
│  • gemini_plan(objective, requirements)              │
│  • gemini_design(step, context)                      │
│  • gemini_review(diff, done_conditions)              │
│  • gemini_build_spec(design, context)                │
│                                                      │
│  Each tool spawns:                                   │
│    gemini -p "..." -o json -y                        │
│  Captures stdout → returns typed JSON to Claude      │
└─────────────────────────────────────────────────────┘
```

### What replaces Codex

Codex was doing two things:
1. **File I/O + shell commands** → Claude Code does this natively (Write, Edit, Bash, Read)
2. **State management** (`state.json`, `todo.md`, autoloop) → Claude writes these directly, or simplified/removed

The FileOpsREQ protocol and autoloop daemon are no longer needed in this architecture.

---

## 4. MCP Server: Tools Design

### 4.1 `gemini_plan`
**Purpose**: Replace Claude's all-plan/tp flow
**Input**:
```json
{
  "objective": "string",
  "requirements": "string",
  "non_goals": "string (optional)"
}
```
**Output**:
```json
{
  "taskName": "string",
  "steps": [
    { "id": "S1", "title": "string", "description": "string", "dependencies": [] }
  ],
  "doneWhen": "string"
}
```
**Claude cost**: ~50 output tokens (tool call args) + ~300 input tokens (read result)

---

### 4.2 `gemini_design`
**Purpose**: Replace Claude's step 2.1 independent design
**Input**:
```json
{
  "step_title": "string",
  "step_description": "string",
  "objective": "string",
  "relevant_files": ["string"],
  "dependencies_done": ["string"]
}
```
**Output**:
```json
{
  "approach": "string",
  "files_to_create": ["string"],
  "files_to_modify": ["string"],
  "commands_to_run": ["string"],
  "done_conditions": ["string (max 2)"],
  "risks": ["string"],
  "needs_split": false,
  "split_reason": null,
  "proposed_substeps": null
}
```
**Claude cost**: ~50 output tokens (tool call args) + ~200 input tokens (read result)

---

### 4.3 `gemini_build_spec`
**Purpose**: Replace Claude's step 4 — turn design into concrete implementation spec
**Input**:
```json
{
  "design": "{ the gemini_design output }",
  "repo_context": "string (relevant file contents, max 2000 chars)",
  "step_title": "string"
}
```
**Output**:
```json
{
  "implementation_steps": [
    { "action": "create|edit|run", "target": "path or command", "content_hint": "string" }
  ],
  "notes": "string"
}
```
**Claude cost**: ~60 output tokens (tool call args) + ~250 input tokens (read result)

---

### 4.4 `gemini_review`
**Purpose**: Replace Claude's step 8 review
**Input**:
```json
{
  "step_title": "string",
  "done_conditions": ["string"],
  "changed_files": ["string"],
  "diff_summary": "string (git diff output, max 3000 chars)",
  "execution_output": "string (command results)"
}
```
**Output**:
```json
{
  "verdict": "PASS|FIX|BLOCKED",
  "done_conditions_met": [{ "condition": "string", "met": true, "evidence": "string" }],
  "issues": [{ "severity": "high|medium|low", "description": "string", "fix": "string" }],
  "summary": "string (2-3 sentences)"
}
```
**Claude cost**: ~80 output tokens (tool call args) + ~200 input tokens (read result)

---

## 5. New `/tr` Flow (Claude + Gemini MCP, No Codex)

### Step 1: Read State
- Claude reads `.ccb/state.json` directly via Read tool
- Validates current step, increments attempts, writes back via Write tool
- ~100 output tokens

### Step 2: Design
- Claude calls `gemini_design(step, context)` MCP tool
- Claude reads result, checks for `needs_split` flag
- If split: Claude updates `state.json` with substeps, stops
- ~50 output tokens (tool call) + ~200 input tokens (read result)

### Step 3: Build Spec (optional)
- If step is complex: Claude calls `gemini_build_spec(design, repo_context)` MCP tool
- Claude reads implementation steps
- ~60 output tokens + ~250 input tokens

### Step 4: Execute
- Claude follows the spec using its own tools:
  - `Write` — create new files
  - `Edit` — modify existing files
  - `Bash` — run commands
- ~100–200 output tokens per file operation (mechanical, not reasoning)

### Step 5: Review
- Claude calls `gemini_review(diff, conditions, ...)` MCP tool
- Claude reads structured verdict
- Claude makes final PASS/FIX decision based on `verdict` field (~100 output tokens)

### Step 6: Finalize
- Claude writes updated `state.json` (status: done, advance current)
- Claude regenerates `todo.md` from state
- ~100 output tokens

### Estimated Claude output per step
| Activity | Output Tokens |
|----------|--------------|
| Read + update state.json | ~100 |
| `gemini_design` tool call | ~50 |
| `gemini_build_spec` tool call (if used) | ~60 |
| File operations (Write/Edit/Bash) | ~150 |
| `gemini_review` tool call | ~80 |
| Final PASS/FIX decision + finalize | ~100 |
| **Total** | **~540 output tokens/step** |

**Reduction**: ~2300–3800 → ~540 = **~85% output token reduction**

---

## 6. New `/tp` (Planning) Flow

### Old flow: Claude-heavy
- Claude runs all-plan internally (~3000–6000 output tokens)

### New flow
1. Claude collects requirements (asks user 3-5 questions) — ~200 output tokens
2. Claude calls `gemini_plan(objective, requirements)` MCP tool — ~50 output tokens
3. Claude reads plan JSON, may tweak 1-2 steps — ~100 output tokens
4. Claude writes `state.json` and `todo.md` directly — ~100 output tokens

**Total Claude output for planning**: ~450 tokens (vs 3000–6000 before)

---

## 7. New `/review` Flow

### Old flow
1. Claude full assessment (~500–800 output tokens)
2. Cross-review by Codex
3. Claude final decision

### New flow
1. Claude calls `gemini_review(...)` MCP tool (~80 output tokens)
2. Claude reads structured verdict (input tokens only — cheap)
3. Claude PASS/FIX decision based on `verdict` field (~100 output tokens)

**Total Claude output for review**: ~180 tokens (vs 800–1200 before)

---

## 8. MCP Server Implementation

### Technology options
| Option | Pros | Cons |
|--------|------|------|
| **Python** | Simple subprocess, easy JSON parsing | Needs Python env |
| **Node.js** | Matches existing Gemini CLI ecosystem | More boilerplate |

Python recommended — simple, minimal dependencies.

### Gemini CLI invocation pattern
```bash
# Basic non-interactive call
gemini -p "PROMPT_HERE" -o json -y

# With stdin for longer prompts
echo "LONG_CONTEXT" | gemini -p "INSTRUCTION" -o json -y

# With specific model
gemini -p "PROMPT" -o json -y -m gemini-2.5-pro
```

Key flags:
- `-p` — headless non-interactive mode (required)
- `-o json` — structured JSON output (avoids text parsing)
- `-y` — auto-approve all tool uses (file reads, etc.)
- `-m` — model override (optional)

### Server structure
```
gemini-mcp-server/
├── server.py          # MCP server entrypoint
├── tools/
│   ├── plan.py        # gemini_plan tool
│   ├── design.py      # gemini_design tool
│   ├── build_spec.py  # gemini_build_spec tool
│   └── review.py      # gemini_review tool
├── prompts/
│   ├── plan.txt       # Prompt template for planning
│   ├── design.txt     # Prompt template for design
│   ├── build_spec.txt # Prompt template for spec building
│   └── review.txt     # Prompt template for review
└── config.json        # Model, timeout, output schema settings
```

### Each tool's internal flow
```
1. Receive tool input (JSON from Claude)
2. Load prompt template from prompts/
3. Inject input values into template
4. Spawn: gemini -p "<filled template>" -o json -y
5. Capture stdout
6. Parse JSON from output
7. Validate against expected schema
8. Return to Claude
```

### Prompt template example (`prompts/design.txt`)
```
You are a software design expert. Design the implementation for this step.

Step: {step_title}
Description: {step_description}
Objective: {objective}
Relevant files: {relevant_files}
Done when: {dependencies_done}

Return ONLY valid JSON matching this schema exactly:
{
  "approach": "string",
  "files_to_create": ["string"],
  "files_to_modify": ["string"],
  "commands_to_run": ["string"],
  "done_conditions": ["string (max 2 items)"],
  "risks": ["string"],
  "needs_split": false,
  "split_reason": null,
  "proposed_substeps": null
}
```

### MCP server registration in Claude settings
```json
{
  "mcpServers": {
    "gemini-builder": {
      "command": "python",
      "args": ["path/to/gemini-mcp-server/server.py"],
      "env": {}
    }
  }
}
```

---

## 9. What Stays, What Goes, What Changes

### Stays (unchanged)
- WezTerm layout (Claude pane + Gemini pane)
- `ask gemini` / `gask` daemon — for manual queries in Gemini pane
- `.ccb/state.json` schema — Claude reads/writes directly
- `.ccb/todo.md` — Claude regenerates directly
- `autoloop.py` — optional, can keep for auto-triggering `/tr`

### Goes (no longer needed)
- Codex pane in WezTerm
- FileOpsREQ protocol (`autoflow.fileops.v1`)
- `autoflow_state_preflight` / `autoflow_state_finalize` ops
- `ask codex` / `cask` daemon for file execution
- Skill files: `file-op/SKILL.md` (Codex-specific)

### Changes
| Component | Old | New |
|-----------|-----|-----|
| `tr/SKILL.md` | Delegates to Codex via FileOpsREQ | Calls MCP tools, Claude does file ops |
| `tp/SKILL.md` | Calls all-plan (Claude-heavy) | Calls `gemini_plan` MCP tool |
| `review/SKILL.md` | Claude full review | Calls `gemini_review` MCP tool |
| `ccb.config` | `codex,gemini,opencode,claude` | `gemini,claude` |
| `roles.json` | executor: codex, reviewer: codex | Not needed (MCP handles routing) |

---

## 10. WezTerm Pane Behavior

### MCP calls (automated flow)
- Claude calls MCP tools → Gemini runs as **headless subprocess**
- No Gemini pane activity for these calls
- Subprocess runs in background, typically 2–5 seconds per call

### Manual queries (unchanged)
- User or Claude can still use `ask gemini "..."` in the Gemini WezTerm pane
- `pend gemini` still works for reading replies
- Useful for ad-hoc questions, debugging, one-off tasks

### Optional: MCP activity log pane
- The MCP server can write a log file: `~/.ccb/gemini-mcp.log`
- WezTerm can display this in a split pane with `tail -f`
- Provides visibility into what Gemini is processing without a full pane

---

## 11. Token Savings Summary

| Workflow | Before (output tokens) | After (output tokens) | Reduction |
|----------|------------------------|----------------------|-----------|
| Per `/tr` step | ~2300–3800 | ~540 | **~85%** |
| Per `/tp` plan | ~3000–6000 | ~450 | **~87%** |
| Per `/review` | ~800–1200 | ~180 | **~83%** |

### 5-step task comparison
| | Before | After |
|--|--------|-------|
| Planning | ~4500 avg | ~450 |
| 5 × execution steps | ~15,000 avg | ~2700 |
| Final review | ~1000 avg | ~180 |
| **Total** | **~20,500 tokens** | **~3,330 tokens** |
| **Saving** | | **~84%** |

Note: Input tokens increase slightly (reading Gemini's JSON results back), but input tokens cost ~5–10x less than output tokens. Net cost reduction is still ~80%+.

---

## 12. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Gemini `-o json` output not parseable | Medium | Validate schema in MCP server; retry with correction prompt |
| Gemini CLI startup adds latency (~2s/call) | Low | Acceptable for design/review ops; not in hot path |
| Gemini design quality lower than Claude | Low-Medium | Claude still reviews design before executing; can override |
| MCP server crashes mid-task | Low | Claude detects tool error, falls back to manual or restarts server |
| Gemini `-y` auto-approves file reads in subprocess | Low | Subprocess runs in isolated context; only reads what prompt says |
| `state.json` corruption if Claude write fails | Low | Claude reads current state before every write; atomic updates |

---

## 13. Implementation Phases

### Phase 0: Validate Gemini CLI headless quality (before any building)
```bash
# Test design output
gemini -p "Design a Python function that validates email addresses. Return JSON: {approach, files_to_create, done_conditions, risks, needs_split}" -o json -y

# Test review output
gemini -p "Review this diff: [paste diff]. Return JSON: {verdict, issues, summary}" -o json -y

# Test planning output
gemini -p "Create a 3-step plan to build a REST API. Return JSON: {taskName, steps: [{id, title, description}]}" -o json -y
```

Criteria: All three return valid parseable JSON with expected fields → proceed.

### Phase 1: Build MCP Server (minimal — design + review only)
- Implement `gemini_design` and `gemini_review` tools
- Register in Claude's `settings.json`
- Test manually: call tools from Claude, verify results
- Do NOT modify skill files yet

### Phase 2: Update skill files
- `tr/SKILL.md` — use MCP tools for steps 2, 4, 8
- `review/SKILL.md` — use `gemini_review` MCP tool
- Test with a real task

### Phase 3: Add planning tool + full integration
- Implement `gemini_plan` tool
- Update `tp/SKILL.md` to use `gemini_plan`
- Remove Codex from `ccb.config`
- Clean up unused skills (`file-op`, Codex-specific logic)

---

*Plan updated: 2026-04-11. No code changes made. Based on: Claude + Gemini only, MCP Server with Gemini CLI subprocess (`gemini -p -o json -y`), Claude handles file ops natively.*
