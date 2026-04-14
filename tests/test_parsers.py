"""Tests for gemini_mcp.core.parsers — pure functions, no I/O."""
import json
import pytest
from gemini_mcp.core.parsers import _find_matching_close, extract_json, truncate


# ── _find_matching_close ──────────────────────────────────────────────────────

class TestFindMatchingClose:
    def test_simple_object(self):
        assert _find_matching_close('{"a": 1}', 0) == 7

    def test_simple_array(self):
        assert _find_matching_close('[1, 2, 3]', 0) == 8

    def test_nested_object(self):
        assert _find_matching_close('{"a": {"b": 1}}', 0) == 14

    def test_nested_array(self):
        assert _find_matching_close('[[1, 2], [3]]', 0) == 12

    def test_brace_inside_string_ignored(self):
        # The } inside the string value must not close the outer object
        s = '{"key": "val}ue"}'
        assert _find_matching_close(s, 0) == len(s) - 1

    def test_escaped_quote_in_string(self):
        s = r'{"key": "val\"ue"}'
        assert _find_matching_close(s, 0) == len(s) - 1

    def test_non_zero_start(self):
        s = 'prefix{"a":1}suffix'
        start = s.index("{")
        assert _find_matching_close(s, start) == s.index("}")

    def test_no_match_returns_minus_one(self):
        assert _find_matching_close("{unclosed", 0) == -1

    def test_empty_object(self):
        assert _find_matching_close("{}", 0) == 1

    def test_empty_array(self):
        assert _find_matching_close("[]", 0) == 1


# ── extract_json ──────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_clean_object(self):
        assert extract_json('{"a": 1}') == '{"a": 1}'

    def test_clean_array(self):
        result = extract_json('[1, 2, 3]')
        assert json.loads(result) == [1, 2, 3]

    def test_markdown_fence_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_markdown_fence_plain(self):
        assert extract_json('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_leading_prose(self):
        result = extract_json('Here is the result: {"status": "ok"}')
        assert json.loads(result) == {"status": "ok"}

    def test_trailing_prose(self):
        result = extract_json('{"status": "ok"} Hope that helps!')
        assert json.loads(result) == {"status": "ok"}

    def test_smiley_brace_edge_case(self):
        # Classic rfind failure: trailing :} should not be mistaken for closing brace
        result = extract_json('Here: {"status": "ok"} Hope that helps! :}')
        assert json.loads(result) == {"status": "ok"}

    def test_brace_in_string_value(self):
        result = extract_json('{"msg": "use {} syntax"}')
        assert json.loads(result) == {"msg": "use {} syntax"}

    def test_nested_json(self):
        s = '{"outer": {"inner": 42}}'
        result = extract_json(s)
        assert json.loads(result) == {"outer": {"inner": 42}}

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="No JSON found"):
            extract_json("   ")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            extract_json("")

    def test_plain_text_raises(self):
        with pytest.raises(ValueError, match="No JSON found"):
            extract_json("no json here at all")

    def test_unmatched_bracket_raises(self):
        with pytest.raises(ValueError):
            extract_json("{unclosed bracket")

    def test_array_with_objects(self):
        s = '[{"a": 1}, {"b": 2}]'
        result = extract_json(s)
        assert json.loads(result) == [{"a": 1}, {"b": 2}]


# ── truncate ──────────────────────────────────────────────────────────────────

class TestTruncate:
    def test_short_string_unchanged(self):
        assert truncate("hello", 100, "test") == "hello"

    def test_none_returns_not_provided(self):
        assert truncate(None, 100, "test") == "not provided"

    def test_exact_limit_unchanged(self):
        s = "a" * 100
        assert truncate(s, 100, "test") == s

    def test_one_over_limit_truncated(self):
        s = "a" * 101
        result = truncate(s, 100, "label")
        assert result.startswith("a" * 100)
        assert "truncated at 100 chars" in result

    def test_truncated_suffix_contains_label(self):
        result = truncate("x" * 200, 50, "myLabel")
        assert "myLabel" in result

    def test_original_content_preserved_up_to_limit(self):
        s = "abcdefghij"
        result = truncate(s, 5, "t")
        assert result.startswith("abcde")
