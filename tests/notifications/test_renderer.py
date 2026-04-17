"""Unit tests for the template renderer (variable substitution, if-blocks, clamping)."""

from __future__ import annotations

import pytest

from src.notifications.templates.renderer import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TemplateRenderError,
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
