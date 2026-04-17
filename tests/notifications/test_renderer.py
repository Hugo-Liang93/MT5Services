"""Unit tests for the template renderer (variable substitution, if-blocks, clamping)."""

from __future__ import annotations

import pytest

from src.notifications.templates.renderer import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TemplateRenderError,
    escape_markdown,
    extract_required_vars,
    render_template,
    truncate_for_telegram,
)


class TestVariableSubstitution:
    def test_flat_var(self):
        assert render_template("hello {{ name }}", {"name": "world"}) == "hello world"

    def test_dotted_var_from_mapping(self):
        result = render_template(
            "sym={{ payload.symbol }}",
            {"payload": {"symbol": "XAUUSD"}},
        )
        assert result == "sym=XAUUSD"

    def test_dotted_var_from_attribute(self):
        class Obj:
            symbol = "XAUUSD"

        result = render_template("sym={{ obj.symbol }}", {"obj": Obj()})
        assert result == "sym=XAUUSD"

    def test_missing_var_raises(self):
        with pytest.raises(TemplateRenderError):
            render_template("hi {{ missing }}", {})

    def test_none_value_becomes_empty_string(self):
        assert render_template("x={{ v }}", {"v": None}) == "x="

    def test_integer_stringified(self):
        assert render_template("n={{ v }}", {"v": 42}) == "n=42"


class TestConditionalBlocks:
    def test_truthy_renders_body(self):
        template = "{% if flag %}yes{% endif %}"
        assert render_template(template, {"flag": True}) == "yes"

    def test_falsy_drops_body(self):
        template = "{% if flag %}yes{% endif %}"
        assert render_template(template, {"flag": False}) == ""

    def test_missing_optional_var_drops_block(self):
        # Missing vars inside {% if %} are treated as falsy; no exception.
        template = "prefix {% if optional %}inner{% endif %} suffix"
        result = render_template(template, {})
        assert "inner" not in result
        assert "prefix" in result and "suffix" in result

    def test_nested_var_inside_if(self):
        template = "{% if flag %}value={{ x }}{% endif %}"
        assert render_template(template, {"flag": True, "x": 7}) == "value=7"


class TestLengthClamping:
    def test_short_passthrough(self):
        assert render_template("hi", {}) == "hi"

    def test_exactly_max_length_passthrough(self):
        payload = "x" * TELEGRAM_MAX_MESSAGE_LENGTH
        result = render_template(payload, {})
        assert len(result) == TELEGRAM_MAX_MESSAGE_LENGTH

    def test_over_max_truncates_with_marker(self):
        payload = "y" * (TELEGRAM_MAX_MESSAGE_LENGTH + 50)
        result = render_template(payload, {})
        assert len(result) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert "truncated" in result

    def test_custom_max_length(self):
        result = truncate_for_telegram("abcdefghij", max_length=5)
        assert len(result) == 5

    def test_truncate_returns_as_is_when_within_limit(self):
        result = truncate_for_telegram("abc", max_length=10)
        assert result == "abc"


class TestMarkdownEscape:
    """These tests lock in the escape-by-default policy. Breaking them means
    dynamic values could leak unescaped into Telegram and trigger
    ``Bad Request: can't parse entities`` (400) in production — a class of
    bug that silently routes CRITICAL alerts to DLQ."""

    def test_escape_underscore(self):
        assert escape_markdown("trend_h1") == r"trend\_h1"

    def test_escape_asterisk(self):
        assert escape_markdown("foo*bar") == r"foo\*bar"

    def test_escape_backtick(self):
        assert escape_markdown("code`block") == "code\\`block"

    def test_escape_bracket(self):
        assert escape_markdown("[tag]") == r"\[tag]"

    def test_escape_all_meta_chars(self):
        assert escape_markdown("a_b*c`d[e") == r"a\_b\*c\`d\[e"

    def test_plain_text_unchanged(self):
        assert escape_markdown("XAUUSD") == "XAUUSD"
        assert escape_markdown("hello world 123") == "hello world 123"

    def test_render_template_escapes_vars_by_default(self):
        result = render_template("strategy={{ name }}", {"name": "trend_h1"})
        assert result == r"strategy=trend\_h1"

    def test_render_template_preserves_template_meta_chars(self):
        # Template's literal *bold* must survive — the escape only targets
        # substituted values, not the template body.
        result = render_template("*bold* {{ x }}", {"x": "safe"})
        assert result == "*bold* safe"

    def test_render_template_escape_false_passthrough(self):
        result = render_template(
            "strategy={{ name }}", {"name": "trend_h1"}, escape_values=False
        )
        assert result == "strategy=trend_h1"


class TestExtractRequiredVars:
    def test_collects_variables(self):
        template = "hi {{ name }} and {{ payload.symbol }}"
        vars_found = extract_required_vars(template)
        assert "name" in vars_found
        assert "payload.symbol" in vars_found

    def test_collects_if_condition_vars(self):
        template = "{% if optional %}x{% endif %} {{ name }}"
        vars_found = extract_required_vars(template)
        assert {"optional", "name"}.issubset(vars_found)
