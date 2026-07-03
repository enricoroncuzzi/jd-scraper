import re
from markdown_it import MarkdownIt
from mdit_py_plugins.deflist import deflist_plugin
from src.tailor.icons import ICONS

_ICON_RE = re.compile(r'<span class="iconify" data-icon="([^"]+)"></span>')

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 12mm 14mm; }}
body {{ margin: 0; }}
#vue-smart-pages-preview {{ font-family: Tahoma, sans-serif; font-size: 13px; }}
{css}
</style></head>
<body><div id="vue-smart-pages-preview">{body}</div></body></html>"""

_HEADER_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
_HEADER_ITEM_SPLIT_RE = re.compile(r"\n[ \t]*:[ \t]")
_DL_RE = re.compile(r"<dl>(.*?)</dl>", re.DOTALL)
_DT_RE = re.compile(r"(<dt>.*?</dt>)", re.DOTALL)


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


def _split_header_region(cv_markdown: str):
    """Return (header_md, body_md) if cv_markdown starts with a level-1
    heading and contains a later level-2 heading; otherwise None."""
    if not cv_markdown.startswith("# "):
        return None

    lines = cv_markdown.split("\n")
    h2_index = None
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if line.startswith("## "):
            h2_index = i
            break
    if h2_index is None:
        return None

    header_md = "\n".join(lines[:h2_index])
    body_md = "\n".join(lines[h2_index:])
    return header_md, body_md


def _render_header(header_md: str, md: MarkdownIt) -> str:
    name_line, _, rest = header_md.partition("\n")
    name = name_line[2:].strip()  # strip leading "# "
    rest = rest.strip("\n")

    paragraphs = []
    if rest:
        for raw_para in _HEADER_PARA_SPLIT_RE.split(rest):
            para = raw_para.strip()
            if para:
                paragraphs.append(para)

    row_html_parts = []
    for para in paragraphs:
        items = [
            item.strip()
            for item in _HEADER_ITEM_SPLIT_RE.split(para)
            if item.strip()
        ]
        item_html_parts = []
        for i, item in enumerate(items):
            css_class = "resume-header-item"
            if i == len(items) - 1:
                css_class += " no-separator"
            inline_html = md.renderInline(item)
            item_html_parts.append(f'<span class="{css_class}">{inline_html}</span>')
        row_html_parts.append(
            f'<div class="resume-header-row">{"".join(item_html_parts)}</div>'
        )

    return f'<div class="resume-header"><h1>{name}</h1>{"".join(row_html_parts)}</div>'


def _split_multi_dt_dl(html: str) -> str:
    """Split any <dl> that contains more than one <dt> into multiple <dl>
    elements, each with exactly one <dt> and the <dd>(s) that follow it up
    to the next <dt>. Leaves single-<dt> <dl>s (e.g. Experience role
    headers) untouched."""

    def repl(match: "re.Match[str]") -> str:
        content = match.group(1)
        if content.count("<dt>") <= 1:
            return f"<dl>{content}</dl>"

        groups = []
        current = None
        for part in _DT_RE.split(content):
            if part.startswith("<dt>"):
                if current is not None:
                    groups.append(current)
                current = part
            elif current is not None:
                current += part
        if current is not None:
            groups.append(current)

        return "".join(f"<dl>{group}</dl>" for group in groups)

    return _DL_RE.sub(repl, html)


def markdown_to_html(cv_markdown: str, css: str) -> str:
    md = MarkdownIt("commonmark", {"html": True}).use(deflist_plugin)

    split = _split_header_region(cv_markdown)
    if split is None:
        body = md.render(cv_markdown)
    else:
        header_md, body_md = split
        header_html = _render_header(header_md, md)
        body_html = md.render(body_md)
        body = header_html + body_html

    body = _split_multi_dt_dl(body)
    body = _inline_icons(body)
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
