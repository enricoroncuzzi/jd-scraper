import os
import pytest

from src.tailor.render_pdf import markdown_to_html

CSS = "#vue-smart-pages-preview { color: black; }"


def test_html_wraps_in_preview_div():
    html = markdown_to_html("# Hi\n\nWorld", CSS)
    assert 'id="vue-smart-pages-preview"' in html
    assert "<h1>Hi</h1>" in html
    assert CSS in html


def test_html_inlines_known_icon():
    md = '<span class="iconify" data-icon="tabler:mail"></span> hi'
    html = markdown_to_html(md, CSS)
    assert "<svg" in html
    assert 'data-icon="tabler:mail"' not in html  # replaced, not left as span


def test_html_renders_definition_list():
    md = "**AI Engineer**\n  : **Hey-Movo**\n  : **Jun 2026**"
    html = markdown_to_html(md, CSS)
    assert "<dl>" in html and "<dd>" in html


MASTER_PATH = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/CV_master.md"
CSS_PATH = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/cv_css.md"


@pytest.mark.skipif(not os.path.exists(MASTER_PATH), reason="master CV not present")
def test_master_renders_to_single_page(tmp_path):
    from src.tailor.render_pdf import render_pdf, pdf_page_count

    md = open(MASTER_PATH).read()
    css = open(CSS_PATH).read()
    out = str(tmp_path / "cv.pdf")
    render_pdf(md, css, out)
    assert os.path.exists(out)
    assert pdf_page_count(out) == 1
