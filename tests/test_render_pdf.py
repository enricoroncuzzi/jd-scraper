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


def test_cover_letter_html_is_byte_identical_to_the_legacy_template():
    from src.tailor.cl_master import parse_cl_master
    from src.tailor.render_pdf import cover_letter_html
    from tests.tailor_helpers import EXAMPLE_CL_MASTER

    letterhead = parse_cl_master(EXAMPLE_CL_MASTER).letterhead
    html = cover_letter_html("Dear Acme team,\n\nI built X.\n\nLooking forward.", letterhead)

    assert html == (
        '<!doctype html>\n'
        '<html><head><meta charset="utf-8"><style>\n'
        "@page { size: A4; margin: 0; }\n"
        "body { margin: 0; font-family: Helvetica, Arial, sans-serif; color: #1f2937; }\n"
        ".page { padding: 64px 74px; }\n"
        '.name { text-align: center; font-family: Georgia, "Times New Roman", serif; '
        "font-weight: bold; font-size: 27px; letter-spacing: 1px; color: #111827; }\n"
        ".tagline { text-align: center; color: #6b7280; font-size: 12px; margin-top: 5px; }\n"
        ".contact { text-align: center; color: #6b7280; font-size: 10.5px; margin-top: 6px; }\n"
        ".rule { border: none; border-top: 1px solid #d1d5db; margin: 16px 0 0; }\n"
        ".title { text-align: center; font-family: Georgia, serif; font-weight: bold; "
        "font-size: 15px; letter-spacing: 3px; margin: 22px 0 18px; color: #111827; }\n"
        ".body p { font-size: 12px; line-height: 1.6; margin: 0 0 13px; text-align: left; }\n"
        ".sign { font-size: 12px; margin-top: 20px; color: #111827; }\n"
        '</style></head>\n<body><div class="page">\n'
        '  <div class="name">Alex Doe</div>\n'
        '  <div class="tagline">AI / ML Engineer | LLM &amp; Agentic Systems | Production ML</div>\n'
        '  <div class="contact">alex.doe@example.com &nbsp;&middot;&nbsp; '
        "(+00) 000 000 0000 &nbsp;&middot;&nbsp; linkedin.com/in/example &nbsp;&middot;&nbsp; "
        "Italy</div>\n"
        '  <hr class="rule">\n'
        '  <div class="title">COVER LETTER</div>\n'
        '  <div class="body"><p>Dear Acme team,</p>\n<p>I built X.</p>\n'
        "<p>Looking forward.</p></div>\n"
        '  <div class="sign">Alex Doe</div>\n'
        "</div></body></html>"
    )
    assert "&amp; Agentic" in html          # "&" is escaped once
    assert "&amp;nbsp;" not in html         # the separator is html, not escaped text


def test_render_pdf_returns_the_fitted_font_size(tmp_path, monkeypatch):
    # The trace records the size the CV was actually set in, so render_pdf has to hand it
    # back instead of swallowing it. No browser: the page is a stub that reports a height
    # too tall until the body font reaches 14px.
    sync_api = pytest.importorskip("playwright.sync_api")
    from src.tailor.render_pdf import render_pdf, _PREVIEW_HEIGHT_JS

    class _Page:
        font = None

        def set_content(self, html, wait_until=None):
            pass

        def emulate_media(self, media=None):
            pass

        def evaluate(self, js):
            if js.startswith("document.body.style.fontSize"):
                self.font = js.split("'")[1].removesuffix("px")
                return None
            assert js == _PREVIEW_HEIGHT_JS
            return 1200 if self.font != "14.0" else 1100

        def pdf(self, **kwargs):
            pass

    class _Browser:
        page = _Page()

        def new_page(self, **kwargs):
            return self.page

        def close(self):
            pass

    class _Chromium:
        browser = _Browser()

        def launch(self):
            return self.browser

    class _Playwright:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Playwright())

    assert render_pdf("# x", "", str(tmp_path / "o.pdf")) == 14.0
