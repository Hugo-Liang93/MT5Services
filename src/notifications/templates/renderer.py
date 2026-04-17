"""Minimal Markdown template renderer.

Supports a deliberately tiny subset so we don't pull Jinja2 for a handful of
notification templates:

- ``{{ var }}`` substitution (supports dotted paths like ``{{ payload.symbol }}``)
- ``{% if <var> %}...{% endif %}`` conditional blocks (single variable, truthy test)

Design goals:
- **Strict**: missing required variables raise. Silent fallbacks would ship
  half-rendered alerts to operators.
- **Safe defaults**: Telegram ``MarkdownV2`` metacharacters are NOT auto-escaped —
  templates are authored deliberately. Payload values that could contain user
  text (symbol tickers, strategy names) are safe; free-form messages should be
  escaped at the classifier layer before handoff.
- **Length-aware**: renderer truncates to Telegram's 4096 char limit with an
  explicit ellipsis marker, rather than failing silently.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_TRUNCATION_MARKER = "\n\n…[truncated]"

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*\}\}")
_IF_BLOCK_PATTERN = re.compile(
    r"\{\%\s*if\s+([a-zA-Z_][a-zA-Z0-9_\.]*)\s*\%\}(.*?)\{\%\s*endif\s*\%\}",
    flags=re.DOTALL,
)


class TemplateRenderError(Exception):
    """Raised when template rendering fails (missing vars, bad syntax)."""


def _resolve_dotted(context: Mapping[str, Any], path: str) -> Any:
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        elif hasattr(current, segment):
            current = getattr(current, segment)
        else:
            raise TemplateRenderError(f"variable '{path}' not in context")
    return current


def _render_if_blocks(template: str, context: Mapping[str, Any]) -> str:
    def _replace(match: "re.Match[str]") -> str:
        var_path = match.group(1)
        body = match.group(2)
        try:
            value = _resolve_dotted(context, var_path)
        except TemplateRenderError:
            return ""  # missing optional var in {% if %} => omit block
        return body if value else ""

    # Repeat until stable to handle nested blocks (rare but supported).
    previous = None
    result = template
    while previous != result:
        previous = result
        result = _IF_BLOCK_PATTERN.sub(_replace, result)
    return result


def _render_variables(template: str, context: Mapping[str, Any]) -> str:
    def _replace(match: "re.Match[str]") -> str:
        path = match.group(1)
        value = _resolve_dotted(context, path)
        return "" if value is None else str(value)

    return _VAR_PATTERN.sub(_replace, template)


def extract_required_vars(template: str) -> set[str]:
    """Return the set of top-level variable roots referenced by the template.

    For ``{{ payload.symbol }}`` we return the full dotted path. For
    ``{% if foo %}`` the condition var is included. Used for startup validation.
    """
    vars_found: set[str] = set()
    for match in _VAR_PATTERN.finditer(template):
        vars_found.add(match.group(1))
    for match in _IF_BLOCK_PATTERN.finditer(template):
        vars_found.add(match.group(1))
    return vars_found


def truncate_for_telegram(
    text: str, *, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> str:
    if len(text) <= max_length:
        return text
    cutoff = max_length - len(_TRUNCATION_MARKER)
    if cutoff <= 0:
        return text[:max_length]
    return text[:cutoff] + _TRUNCATION_MARKER


def render_template(
    template: str,
    context: Mapping[str, Any],
    *,
    max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
) -> str:
    """Render ``template`` against ``context`` and clamp to Telegram's length limit.

    Raises ``TemplateRenderError`` if a required ``{{ var }}`` is missing
    from context (the guardrail against half-rendered alerts).
    """
    expanded = _render_if_blocks(template, context)
    rendered = _render_variables(expanded, context)
    return truncate_for_telegram(rendered, max_length=max_length)
