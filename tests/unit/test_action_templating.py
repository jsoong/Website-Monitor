"""The two template languages of the actions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pagewatch.engine.actions.templating import (
    ARG_VARS,
    PATH_VARS,
    TemplateError,
    check_brace_template,
    check_jinja,
    expand,
    render_jinja,
    safe_filename,
    variables_in,
)

WHEN = datetime(2026, 11, 5, 14, 30, 7, tzinfo=UTC)
VALUES = {"id": "7", "name": "Rent: NYC/Lotteries", "change_id": "42", "info1": "", "url": "u"}


def local(dt: datetime) -> datetime:  # a zone five hours behind UTC, as a naive wall clock
    return dt.replace(tzinfo=None).replace(hour=dt.hour - 5)


# -- {var} templates ------------------------------------------------------------------------


def test_expand_substitutes_variables_and_dates_in_local_time() -> None:
    out = expand(
        "{id}-{change_id} {date:%Y-%m-%d %H:%M} {datetime}", VALUES, when=WHEN, to_local=local
    )
    assert out == "7-42 2026-11-05 09:30 20261105-093007"


def test_safe_expansion_turns_values_into_single_path_components() -> None:
    out = expand("out/{name}/{id}.html", VALUES, when=WHEN, safe=True)
    assert out == "out/Rent_ NYC_Lotteries/7.html"
    evil = {**VALUES, "name": "../../etc/passwd"}
    out = expand("a/{name}/b", evil, safe=True)
    assert ".." not in out and out.count("/") == 2  # nothing of the traversal survives


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("a/b\\c", "a_b_c"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("  trailing dots...  ", "trailing dots"),
        ('q"u<o>t|e?*', "q_u_o_t_e__"),
        ("", "_"),
        ("..", "_"),
        ("x" * 200, "x" * 80),
        ("tab\tand\nnewline", "tab_and_newline"),
    ],
)
def test_safe_filename(raw: str, safe: str) -> None:
    assert safe_filename(raw) == safe


def test_braces_can_be_escaped_by_doubling() -> None:
    assert expand("{{literal}} {id}", VALUES) == "{literal} 7"


@pytest.mark.parametrize(
    "template", ["{name.__class__}", "{name[0]}", "{0}", "{ id }", "{na me}", "{}"]
)
def test_attribute_and_index_access_is_never_evaluated(template: str) -> None:
    with pytest.raises(TemplateError):
        expand(template, VALUES, when=WHEN)


def test_unknown_variable_and_stray_brace_are_errors() -> None:
    with pytest.raises(TemplateError, match="unknown variable"):
        expand("{nope}", VALUES)
    with pytest.raises(TemplateError, match="unmatched"):
        expand("a { b", VALUES)
    with pytest.raises(TemplateError, match="unmatched"):
        expand("a } b", VALUES)


def test_date_needs_a_time_and_a_format() -> None:
    with pytest.raises(TemplateError, match="needs a time"):
        expand("{date:%Y}", VALUES)
    with pytest.raises(TemplateError, match="format"):
        expand("{date}", VALUES, when=WHEN)


def test_check_brace_template_knows_each_kind_of_template() -> None:
    check_brace_template("{name}/{date:%Y}/{info3}", PATH_VARS, "path")
    check_brace_template("--url={url} {file_new} {file_old} {file_changes}", ARG_VARS, "arg")
    with pytest.raises(TemplateError, match="file_new"):
        check_brace_template("{file_new}", PATH_VARS, "path")  # only for program arguments
    with pytest.raises(TemplateError):
        check_brace_template("{date}", PATH_VARS, "path")


def test_variables_in() -> None:
    assert variables_in("a {url} {{x}} {file_new}/{date:%Y}") == {"url", "file_new", "date"}


# -- Jinja2 ---------------------------------------------------------------------------------


def test_jinja_renders_and_tojson_escapes() -> None:
    ctx = {"change": {"summary": 'He said "hi"\nnew line'}}
    out = render_jinja('{"s": {{ change.summary|tojson }}}', ctx)
    import json

    assert json.loads(out) == {"s": 'He said "hi"\nnew line'}


def test_jinja_is_strict_about_unknown_names() -> None:
    with pytest.raises(TemplateError, match="unknown variable"):
        render_jinja("{{ bookmark.nmae }}", {"bookmark": {"name": "x"}})


@pytest.mark.parametrize(
    "source",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ self._TemplateReference__context }}",
        "{{ cycler.__init__.__globals__ }}",
        "{{ lipsum.__globals__ }}",
    ],
)
def test_the_sandbox_blocks_attribute_escapes(source: str) -> None:
    with pytest.raises(TemplateError):
        render_jinja(source, {})


def test_check_jinja_reports_the_line() -> None:
    with pytest.raises(TemplateError, match="line 2"):
        check_jinja("ok\n{% if %}", "template")


def test_runaway_range_is_bounded_by_the_sandbox() -> None:
    # the sandbox caps range() so a template cannot allocate without limit
    with pytest.raises(TemplateError):
        render_jinja("{% for i in range(10**9) %}x{% endfor %}", {})
