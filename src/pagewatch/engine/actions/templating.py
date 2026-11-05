"""The two template languages actions use.

``{var}`` templates (spec: export paths, run_program arguments)
    A deliberately tiny language: ``{name}`` is replaced from a fixed set of variables,
    ``{date:<strftime format>}`` formats the change's detection time in the local zone, and
    ``{{`` / ``}}`` are literal braces. It is *not* ``str.format``: an attribute or index
    (``{name.__class__}``, ``{name[0]}``) is an error, never evaluated. Values that end up in a
    file name are made safe first (no separators, no reserved Windows names).

Jinja2 templates (spec: emails, webhooks)
    A ``SandboxedEnvironment`` with ``StrictUndefined``: a typo in a variable name is an error
    the user sees in the Problems list, not an empty string in an email.

This module imports nothing from ``pagewatch.models`` (the models call it to validate).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

# Variables each template kind accepts. ``date`` takes a strftime format: ``{date:%Y-%m-%d}``.
PATH_VARS = frozenset({"id", "name", "datetime", "change_id", "info1", "info2", "info3", "date"})
ARG_VARS = frozenset(
    {"url", "file_new", "file_old", "file_changes", "id", "name", "change_id", "info1", "info2",
     "info3", "datetime"}
)  # fmt: skip

DATETIME_FORMAT = "%Y%m%d-%H%M%S"
MAX_NAME_LEN = 80
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}  # fmt: skip
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TemplateError(ValueError):
    """A template that cannot be used (bad syntax, unknown variable, failed render)."""


# -- {var} templates -----------------------------------------------------------------------


def _tokens(template: str) -> list[tuple[str, str | None, str | None]]:
    """Split into ``("lit", text, None)`` and ``("var", name, fmt)`` pieces."""
    out: list[tuple[str, str | None, str | None]] = []
    pos = 0
    for m in _TOKEN.finditer(template):
        if m.start() > pos:
            out.append(("lit", template[pos : m.start()], None))
        pos = m.end()
        whole = m.group(0)
        if whole == "{{":
            out.append(("lit", "{", None))
        elif whole == "}}":
            out.append(("lit", "}", None))
        elif m.group(1) is None:
            raise TemplateError(
                f"unmatched {whole!r} in {template!r} (write {whole}{whole} for a literal)"
            )
        else:
            body = m.group(1)
            name, sep, fmt = body.partition(":")
            if not _NAME.match(name):
                raise TemplateError(f"{{{body}}} is not a variable name")
            out.append(("var", name, fmt if sep else None))
    if pos < len(template):
        out.append(("lit", template[pos:], None))
    return out


def variables_in(template: str) -> set[str]:
    """The variable names a ``{var}`` template uses."""
    return {name for kind, name, _ in _tokens(template) if kind == "var" and name is not None}


def check_brace_template(template: str, allowed: frozenset[str], what: str) -> None:
    """Raise ``TemplateError`` for an unknown variable or stray brace (used by the models)."""
    for kind, name, fmt in _tokens(template):
        if kind != "var":
            continue
        assert name is not None
        if name not in allowed:
            raise TemplateError(
                f"{what}: unknown variable {{{name}}}; available: "
                + ", ".join("{" + v + "}" for v in sorted(allowed))
            )
        if fmt is not None and name != "date":
            raise TemplateError(f"{what}: only {{date:<format>}} takes a format, not {{{name}}}")
        if name == "date" and not fmt:
            raise TemplateError(f"{what}: {{date}} needs a format, for example {{date:%Y-%m-%d}}")


def safe_filename(value: str) -> str:
    """Make one path component out of arbitrary text (a bookmark name, an info field)."""
    text = _UNSAFE.sub("_", value).strip().rstrip(". ")
    text = re.sub(r"\.{2,}", "_", text)
    text = re.sub(r"\s+", " ", text)[:MAX_NAME_LEN].rstrip(". ")
    if not text:
        return "_"
    if text.split(".")[0].lower() in _RESERVED:
        text = "_" + text
    return text


def expand(
    template: str,
    values: Mapping[str, str],
    *,
    when: datetime | None = None,
    to_local: Any = None,
    safe: bool = False,
) -> str:
    """Substitute ``{var}`` placeholders. ``when`` is the change's detection time (aware UTC);
    ``to_local`` converts it to local wall time (``Zone.to_local``). With ``safe`` every
    substituted value becomes a single safe path component."""
    out: list[str] = []
    for kind, a, fmt in _tokens(template):
        if kind == "lit":
            out.append(a or "")
            continue
        assert a is not None
        if a == "date" or (a == "datetime" and a not in values):
            if when is None:
                raise TemplateError(f"{{{a}}} needs a time but none is available")
            local = to_local(when) if to_local is not None else when
            if a == "date":
                if not fmt:
                    raise TemplateError("{date} needs a format, for example {date:%Y-%m-%d}")
                try:
                    text = local.strftime(fmt)
                except ValueError as exc:
                    raise TemplateError(f"bad date format {fmt!r}: {exc}") from exc
            else:
                text = local.strftime(DATETIME_FORMAT)
        else:
            if fmt is not None:
                raise TemplateError(f"only {{date:<format>}} takes a format, not {{{a}}}")
            if a not in values:
                raise TemplateError(f"unknown variable {{{a}}}")
            text = values[a]
        out.append(safe_filename(text) if safe else text)
    return "".join(out)


# -- Jinja2 ------------------------------------------------------------------------------


def jinja_env(*, autoescape: bool = False) -> SandboxedEnvironment:
    return SandboxedEnvironment(
        autoescape=autoescape,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def check_jinja(source: str, what: str) -> None:
    """Syntax check only (variables are unknown until a change exists)."""
    try:
        jinja_env().parse(source)
    except jinja2.TemplateSyntaxError as exc:
        raise TemplateError(f"{what}: {exc.message} (line {exc.lineno})") from exc


def render_jinja(source: str, context: Mapping[str, Any], *, autoescape: bool = False) -> str:
    try:
        return jinja_env(autoescape=autoescape).from_string(source).render(**context)
    except jinja2.TemplateSyntaxError as exc:
        raise TemplateError(f"template syntax error: {exc.message} (line {exc.lineno})") from exc
    except jinja2.exceptions.SecurityError as exc:
        raise TemplateError(f"template is not allowed to do that: {exc}") from exc
    except jinja2.UndefinedError as exc:
        raise TemplateError(f"template uses an unknown variable: {exc.message}") from exc
    except jinja2.TemplateError as exc:
        raise TemplateError(f"template failed: {exc}") from exc
    except (TypeError, ValueError, ArithmeticError, AttributeError, KeyError) as exc:
        raise TemplateError(f"template failed: {type(exc).__name__}: {exc}") from exc
