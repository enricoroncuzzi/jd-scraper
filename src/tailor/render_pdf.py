import re
from markdown_it import MarkdownIt
from mdit_py_plugins.deflist import deflist_plugin
from src.tailor.icons import ICONS

_ICON_RE = re.compile(r'<span class="iconify" data-icon="([^"]+)"></span>')

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 12mm 14mm; }}
body {{ margin: 0; }}
{css}
</style></head>
<body><div id="vue-smart-pages-preview">{body}</div></body></html>"""


def _strip_css_fence(css: str) -> str:
    m = re.search(r"```css\n(.*?)```", css, re.DOTALL)
    return m.group(1) if m else css


def _inline_icons(html: str) -> str:
    return _ICON_RE.sub(
        lambda m: ICONS.get(m.group(1), m.group(0)).replace(
            "<svg", '<svg class="iconify"'
        ),
        html,
    )


def markdown_to_html(cv_markdown: str, css: str) -> str:
    md = MarkdownIt("commonmark", {"html": True}).use(deflist_plugin)
    body = _inline_icons(md.render(cv_markdown))
    return _HTML_TEMPLATE.format(css=_strip_css_fence(css), body=body)


def render_pdf(cv_markdown: str, css: str, out_path: str) -> None:
    from playwright.sync_api import sync_playwright

    html = markdown_to_html(cv_markdown, css)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(path=out_path, format="A4", print_background=True)
        browser.close()


def pdf_page_count(path: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(path).pages)
