"""PDF, DOCX and XLSX to HTML (spec: Fetch layer, document row)."""

from __future__ import annotations

import pytest

from pagewatch.engine.pipeline import documents
from pagewatch.engine.pipeline.core import build_blocks
from pagewatch.engine.pipeline.documents import DocumentError
from pagewatch.models import FilterConfig
from tests.support.docs import make_docx, make_pdf, make_xlsx

HASH = "0" * 64


def blocks(body: bytes, ctype: str, source: str = "auto") -> list[str]:
    return [
        b.text for b in build_blocks(body, ctype, "https://x.test/f", source, FilterConfig(), HASH)
    ]


PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# -- PDF --------------------------------------------------------------------------------


def test_pdf_text_per_page_one_paragraph_per_text_block() -> None:
    pdf = make_pdf([["Annual report", "Revenue grew 4 percent"], ["Appendix A", "Methods"]])
    assert blocks(pdf, PDF) == ["Annual report", "Revenue grew 4 percent", "Appendix A", "Methods"]


def test_pdf_pages_are_wrapped_in_page_containers() -> None:
    html = documents.pdf_to_html(make_pdf([["one"], ["two"]]))
    assert html.count('class="pw-page"') == 2 and 'data-page="2"' in html


def test_pdf_revision_changes_exactly_the_edited_paragraph() -> None:
    v1 = make_pdf([["Opening hours", "Monday to Friday 9 to 5", "Closed on holidays"]])
    v2 = make_pdf([["Opening hours", "Monday to Saturday 9 to 5", "Closed on holidays"]])
    a, b = blocks(v1, PDF), blocks(v2, PDF)
    assert len(a) == len(b) == 3
    assert [x != y for x, y in zip(a, b, strict=True)] == [False, True, False]


def test_pdf_text_is_escaped_not_interpreted_as_markup() -> None:
    html = documents.pdf_to_html(make_pdf([["<script>alert(1)</script> & more"]]))
    assert "<script>" not in html and "&lt;script&gt;" in html and "&amp; more" in html


def test_pdf_by_extension_when_the_content_type_is_generic() -> None:
    out = build_blocks(
        make_pdf([["hello pdf"]]), "application/octet-stream", "https://x.test/a.pdf", "auto",
        FilterConfig(), HASH,
    )  # fmt: skip
    assert out and "hello pdf" in out[0].text.lower()


def test_pdf_without_text_warns_instead_of_failing() -> None:
    warnings: list[str] = []
    html = documents.pdf_to_html(make_pdf([[]]), warnings.append)
    assert "<p>" not in html and any("no extractable text" in w for w in warnings)


@pytest.mark.parametrize("data", [b"not a pdf at all", b"%PDF-1.4\ngarbage that is not a pdf"])
def test_unreadable_pdf_is_a_document_error(data: bytes) -> None:
    with pytest.raises(DocumentError):
        documents.pdf_to_html(data)


# -- DOCX -------------------------------------------------------------------------------


def test_docx_headings_paragraphs_lists_and_tables_in_document_order() -> None:
    data = make_docx(
        [
            ("h1", "Policy"),
            ("p", "All staff must badge in."),
            ("li", "Front door"),
            ("li", "Garage"),
            ("table", [["Floor", "Badge"], ["1", "Blue"]]),
            ("p", "Questions to HR."),
        ]
    )
    html = documents.docx_to_html(data)
    assert html.index("<h1>Policy</h1>") < html.index("<ul>") < html.index("<table>")
    assert html.index("</table>") < html.index("Questions to HR.")
    got = blocks(data, DOCX)
    assert "Policy" in got and "Front door" in got and "Floor | Badge" in got and "1 | Blue" in got
    assert got.index("Policy") < got.index("Questions to HR.")


def test_docx_edit_changes_one_block() -> None:
    a = blocks(make_docx([("p", "Fee is $10"), ("p", "Due Friday")]), DOCX)
    b = blocks(make_docx([("p", "Fee is $12"), ("p", "Due Friday")]), DOCX)
    assert [x != y for x, y in zip(a, b, strict=True)] == [True, False]


def test_docx_legacy_ole_and_garbage_are_document_errors() -> None:
    with pytest.raises(DocumentError, match=r"\.doc"):
        documents.docx_to_html(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(DocumentError):
        documents.docx_to_html(b"PK\x03\x04 not really a docx")


# -- XLSX -------------------------------------------------------------------------------


def test_xlsx_one_table_per_sheet_with_a_row_per_block() -> None:
    data = make_xlsx(
        {"Prices": [["Item", "Price"], ["Tea", 3.5], ["Cake", 4]], "Notes": [["Open daily"]]}
    )
    html = documents.xlsx_to_html(data)
    assert html.count("<table>") == 2 and "<h2>Prices</h2>" in html and "<h2>Notes</h2>" in html
    got = blocks(data, XLSX)
    assert "Item | Price" in got and "Tea | 3.5" in got and "Cake | 4" in got
    assert "Open daily" in got


def test_xlsx_cell_update_changes_only_that_row() -> None:
    a = blocks(make_xlsx({"S": [["a", 1], ["b", 2], ["c", 3]]}), XLSX)
    b = blocks(make_xlsx({"S": [["a", 1], ["b", 9], ["c", 3]]}), XLSX)
    # the sheet title is its own block, then one block per row
    assert [x != y for x, y in zip(a, b, strict=True)] == [False, False, True, False]


def test_xlsx_is_capped_at_5000_rows_with_a_warning() -> None:
    warnings: list[str] = []
    data = make_xlsx({"Big": [[i, f"row {i}"] for i in range(5200)]})
    html = documents.xlsx_to_html(data, warnings.append)
    assert html.count("<tr>") == documents.MAX_XLSX_ROWS
    assert any("5000" in w for w in warnings)


def test_xlsx_skips_empty_rows_and_trailing_empty_cells() -> None:
    html = documents.xlsx_to_html(make_xlsx({"S": [["a", None, None], [None, None], ["b", "c"]]}))
    assert html.count("<tr>") == 2 and "<td>a</td></tr>" in html


def test_xlsx_legacy_and_garbage_are_document_errors() -> None:
    with pytest.raises(DocumentError, match=r"\.xls"):
        documents.xlsx_to_html(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(DocumentError):
        documents.xlsx_to_html(b"nope")


def test_explicit_source_type_forces_the_conversion() -> None:
    got = blocks(make_xlsx({"S": [["only", "row"]]}), "application/octet-stream", "xlsx")
    assert got == ["S", "only | row"]
