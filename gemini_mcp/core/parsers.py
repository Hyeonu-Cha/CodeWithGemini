from __future__ import annotations

import json

# Truncation limits
MAX_DIFF_CHARS    = 3500
MAX_OUTPUT_CHARS  = 2000
MAX_SPEC_CHARS    = 8000


def truncate(value: str | None, max_chars: int, label: str) -> str:
    if value is None:
        return "not provided"
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n... [{label} truncated at {max_chars} chars]"


def _find_matching_close(text: str, start: int) -> int:
    """Return the index of the bracket that closes the one at text[start].

    Handles nested brackets and quoted strings so that brace characters inside
    string values do not affect the depth count.
    Returns -1 if no matching close is found.
    """
    open_ch  = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth       = 0
    in_string   = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def extract_json(output: str) -> str:
    """Extract a JSON object/array from model output, stripping markdown fences."""
    trimmed = output.strip()

    # Strip ```json ... ``` or ``` ... ``` fences
    if trimmed.startswith("```"):
        first_newline = trimmed.find("\n")
        last_fence    = trimmed.rfind("```")
        if first_newline > 0 and last_fence > first_newline:
            trimmed = trimmed[first_newline + 1:last_fence].strip()

    # Try direct parse first (fast path — no leading/trailing prose)
    try:
        json.loads(trimmed)
        return trimmed
    except json.JSONDecodeError:
        pass

    # Find the first { or [ and match its closing bracket by depth-counting.
    # rfind was previously used here but fails on outputs like:
    #   Here's the result: {"status": "ok"} Hope that helps! :}
    # because rfind("}") picks up the smiley's }, not the JSON's.
    obj_start = trimmed.find("{")
    arr_start = trimmed.find("[")

    if obj_start < 0 and arr_start < 0:
        raise ValueError(f"No JSON found in Gemini output: {trimmed[:200]}")

    if obj_start < 0:
        start = arr_start
    elif arr_start < 0:
        start = obj_start
    else:
        start = min(obj_start, arr_start)

    end = _find_matching_close(trimmed, start)
    if end < 0:
        raise ValueError("Unmatched brackets in Gemini output")

    candidate = trimmed[start:end + 1]
    json.loads(candidate)  # raises JSONDecodeError if still invalid
    return candidate
