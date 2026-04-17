"""Template discovery, validation, and rendering registry.

Templates live under ``config/notifications/templates/*.md``. Each file carries
an HTML comment header describing its severity, tag prefix, and required
variables — this lets us validate at startup that every declared-active event
has a matching template whose vars the classifier actually provides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.notifications.events import NotificationEvent, Severity, severity_from_str
from src.notifications.templates.renderer import extract_required_vars, render_template

logger = logging.getLogger(__name__)

_HEADER_PATTERN = re.compile(
    r"^\s*<!--(?P<header>.*?)-->\s*(?P<body>.*)",
    flags=re.DOTALL,
)
_HEADER_FIELD_PATTERN = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$",
    flags=re.MULTILINE,
)


class TemplateNotFoundError(KeyError):
    """Raised when the requested template key is not registered."""


class TemplateValidationError(ValueError):
    """Raised when a template file fails header validation."""


@dataclass(frozen=True)
class TemplateMetadata:
    key: str
    severity: Severity
    tag: str
    required_vars: frozenset[str]
    body: str
    path: Path


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_header_and_body(raw: str, path: Path) -> tuple[dict[str, str], str]:
    match = _HEADER_PATTERN.match(raw)
    if not match:
        raise TemplateValidationError(
            f"{path}: template must start with an HTML comment header"
        )
    header_text = match.group("header")
    body = match.group("body").lstrip("\n")
    fields: dict[str, str] = {}
    for field_match in _HEADER_FIELD_PATTERN.finditer(header_text):
        fields[field_match.group(1).strip().lower()] = field_match.group(2).strip()
    return fields, body


def _validate_metadata(
    key: str,
    header: Mapping[str, str],
    body: str,
    path: Path,
) -> TemplateMetadata:
    missing = [
        name for name in ("severity", "tag", "required_vars") if name not in header
    ]
    if missing:
        raise TemplateValidationError(
            f"{path}: missing required header fields: {', '.join(missing)}"
        )
    try:
        severity = severity_from_str(header["severity"])
    except ValueError as exc:
        raise TemplateValidationError(f"{path}: {exc}") from exc
    tag = header["tag"].strip()
    if not tag:
        raise TemplateValidationError(f"{path}: tag cannot be empty")
    declared = frozenset(_parse_csv(header["required_vars"]))
    referenced = {
        var.split(".", 1)[0] if "." in var else var
        for var in extract_required_vars(body)
    }
    # Every referenced top-level root must be declared (guardrail against drift
    # between template and classifier output). Extra declared vars are allowed
    # (forward-compat for shared payloads).
    missing_declarations = referenced - declared
    if missing_declarations:
        raise TemplateValidationError(
            f"{path}: template references undeclared vars: "
            f"{sorted(missing_declarations)}"
        )
    return TemplateMetadata(
        key=key,
        severity=severity,
        tag=tag,
        required_vars=declared,
        body=body,
        path=path,
    )


@dataclass
class TemplateRegistry:
    """Loads + serves templates, enforcing startup-time validation."""

    directory: Path
    strict: bool = True
    _templates: dict[str, TemplateMetadata] = field(default_factory=dict)

    @classmethod
    def from_directory(
        cls, directory: str | Path, *, strict: bool = True
    ) -> "TemplateRegistry":
        registry = cls(directory=Path(directory), strict=strict)
        registry.reload()
        return registry

    def reload(self) -> None:
        directory = self.directory
        if not directory.exists():
            if self.strict:
                raise TemplateValidationError(
                    f"template directory does not exist: {directory}"
                )
            logger.warning("template directory missing: %s", directory)
            self._templates = {}
            return
        loaded: dict[str, TemplateMetadata] = {}
        for path in sorted(directory.glob("*.md")):
            key = path.stem
            try:
                raw = path.read_text(encoding="utf-8")
                header, body = _parse_header_and_body(raw, path)
                metadata = _validate_metadata(key, header, body, path)
            except TemplateValidationError:
                if self.strict:
                    raise
                logger.exception("skipping invalid template %s", path)
                continue
            loaded[key] = metadata
        self._templates = loaded
        logger.info(
            "template registry loaded %d templates from %s",
            len(loaded),
            directory,
        )

    def keys(self) -> Iterable[str]:
        return self._templates.keys()

    def get(self, key: str) -> TemplateMetadata:
        try:
            return self._templates[key]
        except KeyError as exc:
            raise TemplateNotFoundError(
                f"no template registered for key: {key!r}"
            ) from exc

    def ensure_keys(self, required: Iterable[str]) -> None:
        """Assert all required keys exist (used at boot when event map is known)."""
        missing = sorted(set(required) - set(self._templates.keys()))
        if missing:
            raise TemplateValidationError(f"template registry missing keys: {missing}")

    def render(
        self,
        key: str,
        context: Mapping[str, Any],
        *,
        add_tag_prefix: bool = True,
    ) -> str:
        metadata = self.get(key)
        missing = metadata.required_vars - set(context.keys())
        if missing:
            raise TemplateValidationError(
                f"rendering {key!r}: context missing required vars: {sorted(missing)}"
            )
        rendered = render_template(metadata.body, context)
        if add_tag_prefix:
            return f"[{metadata.tag}] {rendered}"
        return rendered

    def render_event(self, event: NotificationEvent) -> str:
        context = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "severity": event.severity.value,
            "source": event.source,
            "instance": event.instance,
            "ts": event.ts.isoformat(),
            "payload": event.payload,
            **event.payload,  # Top-level payload fields accessible without prefix.
        }
        return self.render(event.template_key, context)
