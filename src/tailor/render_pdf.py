import re
from html import escape as _html_escape
from markdown_it import MarkdownIt
from mdit_py_plugins.deflist import deflist_plugin
from src.tailor.icons import ICONS

_ICON_RE = re.compile(r'<span class="iconify" data-icon="([^"]+)"></span>')

_DYNAMIC_STYLES = """
body { font-family: Tahoma, sans-serif; font-size: 15px; background-color: white; margin: 0; }
#vue-smart-pages-preview { background-color: white; width: 793px; max-width: 100%; padding: 55px 45px; box-sizing: border-box; }
#vue-smart-pages-preview dl dt { white-space: nowrap; }
:not(.resume-header-item) > a { color: #000000; }
h1, h2, h3 { color: #000000; }
h1, h2 { border-bottom-color: #000000; }
p, li { line-height: 1.30; }
h2, h3 { line-height: 1.50; }
dl { line-height: 1.35; }
h2 { margin-top: 5px; }
@media print {
  body { background-color: white; padding: 0; margin: 0; }
  #vue-smart-pages-preview { width: 100%; max-width: none; box-shadow: none; margin: 0; padding: 55px 45px; }
  @page { size: A4; margin: 0; }
}
"""

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
{css}
{dynamic_styles}
</style></head>
<body><main id="vue-smart-pages-preview"><div class="resume-header"></div>{body}</main></body></html>"""

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

    body = md.render(cv_markdown)
    body = _split_multi_dt_dl(body)
    body = _inline_icons(body)
    return _HTML_TEMPLATE.format(
        css=_strip_css_fence(css), dynamic_styles=_DYNAMIC_STYLES, body=body
    )


# A4 at 96 dpi is 794 x 1123 px. OhMyCV's content is intrinsically taller than
# one A4 page at its native 15px, and its export fits one page via a scale-to-fit
# step (~0.88). We reproduce that by shrinking the base font until the content
# fits one page height at full width — never upscaling beyond the native 15px.
_A4_WIDTH_PX = 794
_A4_HEIGHT_PX = 1123
_FIT_TARGET_PX = 1116  # A4 printable height minus a small safety margin
_MAX_FONT_PX = 15.0
_MIN_FONT_PX = 9.0
_PREVIEW_HEIGHT_JS = (
    "() => document.getElementById('vue-smart-pages-preview')"
    ".getBoundingClientRect().height"
)


def _fit_font_size(page) -> float:
    """Shrink the base font-size until the CV fits one A4 page height, mirroring
    OhMyCV's scale-to-fit export. Never upscales beyond the native 15px."""
    font = _MAX_FONT_PX
    while font >= _MIN_FONT_PX:
        page.evaluate(f"document.body.style.fontSize = '{font}px'")
        height = page.evaluate(_PREVIEW_HEIGHT_JS)
        if height <= _FIT_TARGET_PX:
            break
        font -= 0.5
    return font


def render_pdf(cv_markdown: str, css: str, out_path: str) -> None:
    from playwright.sync_api import sync_playwright

    html = markdown_to_html(cv_markdown, css)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": _A4_WIDTH_PX, "height": _A4_HEIGHT_PX}
        )
        page.set_content(html, wait_until="networkidle")
        page.emulate_media(media="print")
        _fit_font_size(page)
        page.pdf(
            path=out_path,
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        browser.close()


def pdf_page_count(path: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(path).pages)


# --- Cover letter (Enhancv-style letterhead) ---------------------------------
# No dash characters anywhere in the header/signature (space-separated phone,
# "|" and "/" in the tagline, comma in the signature) to keep the whole document
# dash-free, matching the generation style.
_CL_NAME = "Enrico Roncuzzi"
_CL_TAGLINE = "AI / ML Engineer | LLM &amp; Agentic Systems | Production ML"
_CL_CONTACT = (
    "enrico.roncuzzi98@gmail.com &nbsp;&middot;&nbsp; (+39) 334 814 7584 "
    "&nbsp;&middot;&nbsp; linkedin.com/in/enricoroncuzzi &nbsp;&middot;&nbsp; Italy"
)
_CL_SIGNATURE = "Enrico Roncuzzi"

_COVER_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 0; }}
body {{ margin: 0; font-family: Helvetica, Arial, sans-serif; color: #1f2937; }}
.page {{ padding: 64px 74px; }}
.name {{ text-align: center; font-family: Georgia, "Times New Roman", serif; font-weight: bold; font-size: 27px; letter-spacing: 1px; color: #111827; }}
.tagline {{ text-align: center; color: #6b7280; font-size: 12px; margin-top: 5px; }}
.contact {{ text-align: center; color: #6b7280; font-size: 10.5px; margin-top: 6px; }}
.rule {{ border: none; border-top: 1px solid #d1d5db; margin: 16px 0 0; }}
.title {{ text-align: center; font-family: Georgia, serif; font-weight: bold; font-size: 15px; letter-spacing: 3px; margin: 22px 0 18px; color: #111827; }}
.body p {{ font-size: 12px; line-height: 1.6; margin: 0 0 13px; text-align: left; }}
.sign {{ font-size: 12px; margin-top: 20px; color: #111827; }}
</style></head>
<body><div class="page">
  <div class="name">{name}</div>
  <div class="tagline">{tagline}</div>
  <div class="contact">{contact}</div>
  <hr class="rule">
  <div class="title">COVER LETTER</div>
  <div class="body">{body}</div>
  <div class="sign">{signature}</div>
</div></body></html>"""


def compose_cover_letter(company: str, hook: str, bridge: str, proof_text: str) -> str:
    from src.tailor.generate import FIXED_CLOSE

    greeting = f"Dear {company.strip()} team,"
    return "\n\n".join([greeting, hook.strip(), bridge.strip(), proof_text.strip(), FIXED_CLOSE])


def cover_letter_html(cover_text: str) -> str:
    """Wrap the generated cover-letter body (greeting + paragraphs) in the letterhead."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", cover_text.strip()) if p.strip()]
    body = "\n".join(f"<p>{_html_escape(p)}</p>" for p in paras)
    return _COVER_TEMPLATE.format(
        name=_html_escape(_CL_NAME),
        tagline=_CL_TAGLINE,
        contact=_CL_CONTACT,
        body=body,
        signature=_html_escape(_CL_SIGNATURE),
    )


def render_cover_letter_pdf(cover_text: str, out_path: str) -> None:
    from playwright.sync_api import sync_playwright

    html = cover_letter_html(cover_text)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": _A4_WIDTH_PX, "height": _A4_HEIGHT_PX}
        )
        page.set_content(html, wait_until="networkidle")
        page.emulate_media(media="print")
        page.pdf(
            path=out_path,
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        browser.close()
