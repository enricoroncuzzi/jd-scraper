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


def test_header_renders_as_plain_deflists_no_separator_spans():
    md = (
        "# Enrico Roncuzzi\n\n"
        '<span class="iconify" data-icon="tabler:mail"></span> [mail](mailto:x)\n'
        "  : [phone](https://p)\n\n"
        '<span class="iconify" data-icon="tabler:brand-github"></span> [gh](https://g)\n'
        "  : [medium](https://m)\n\n"
        "## Summary\nhi"
    )
    html = markdown_to_html(md, CSS)
    # No markup produces resume-header-item spans (the CSS dynamic-styles
    # block legitimately references the selector `.resume-header-item` for
    # backwards compat, so we check the class attribute form specifically).
    assert 'class="resume-header-item' not in html
    assert " | " not in html
    assert html.count("<dl>") >= 2


def test_html_splits_consecutive_definition_entries_into_separate_dl():
    md = "**A**\n  : **x**\n\n**B**\n  : **y**"
    html = markdown_to_html(md, CSS)
    assert html.count("<dl>") == 2


MASTER_PATH = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/CV_master.md"
CSS_PATH = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/source/CV_css.md"


@pytest.mark.skipif(not os.path.exists(MASTER_PATH), reason="master CV not present")
def test_master_renders_to_single_page(tmp_path):
    from src.tailor.render_pdf import render_pdf, pdf_page_count

    md = open(MASTER_PATH).read()
    css = open(CSS_PATH).read()
    out = str(tmp_path / "cv.pdf")
    render_pdf(md, css, out)
    assert os.path.exists(out)
    assert pdf_page_count(out) == 1


def test_cover_letter_html_letterhead():
    from src.tailor.render_pdf import cover_letter_html

    html = cover_letter_html("Dear Acme team,\n\nI built X.\n\nLooking forward.")
    assert "Enrico Roncuzzi" in html
    assert "AI / ML Engineer" in html
    assert "COVER LETTER" in html
    assert "<p>Dear Acme team,</p>" in html
    assert "<p>I built X.</p>" in html
    assert "334 814 7584" in html        # dash-free phone in the letterhead
    assert "334-814-7584" not in html    # no dash tell


def test_compose_cover_letter_orders_parts_and_fixed_close():
    from src.tailor.render_pdf import compose_cover_letter
    from src.tailor.generate import FIXED_CLOSE

    body = compose_cover_letter(
        hook="I follow how Acme builds travel tools.",
        bridge="At Hey Movo I built a coordinator agent using the Model Context Protocol.",
        proof_text="Built the agentic layer with planner and critic loops.",
    )
    parts = [p.strip() for p in body.split("\n\n") if p.strip()]
    assert parts[0].startswith("I follow how Acme")
    assert "Model Context Protocol" in parts[1]
    assert parts[2] == "Built the agentic layer with planner and critic loops."
    assert parts[3] == FIXED_CLOSE
