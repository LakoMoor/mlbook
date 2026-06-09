#!/usr/bin/env python3
"""
Yandex ML Handbook → PDF builder.
Скачивает все статьи, конвертирует LaTeX в MathML, собирает PDF.

Usage:
    python build_pdf.py            # build (with caching)
    python build_pdf.py --force    # re-fetch all pages
"""

import re
import json
import hashlib
import subprocess
import sys
import time
import os
from pathlib import Path
from urllib.parse import unquote, quote

# On macOS with Homebrew, pango/gobject lives here; harmless on Linux
if sys.platform == "darwin":
    os.environ.setdefault("DYLD_LIBRARY_PATH", "/opt/homebrew/lib")

import latex2mathml.converter
import weasyprint

BASE_URL = "https://education.yandex.ru/handbook/ml"
CACHE_DIR = Path("cache")
PAGE_CACHE = CACHE_DIR / "pages"
IMG_CACHE = CACHE_DIR / "images"
FONT_DIR = CACHE_DIR / "fonts"
OUTPUT_PDF = Path("ml_handbook.pdf")
OUTPUT_HTML = Path("ml_handbook.html")


# ── Font paths ────────────────────────────────────────────────────────────────

def font_url(filename: str) -> str:
    return (FONT_DIR / filename).resolve().as_uri()


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch(url: str, cache_path: Path | None = None, force: bool = False) -> bytes:
    if cache_path and cache_path.exists() and not force:
        return cache_path.read_bytes()
    result = subprocess.run(
        ["curl", "-skL", "--max-time", "60", "--retry", "3",
         "--retry-delay", "2", url],
        capture_output=True,
    )
    data = result.stdout
    if cache_path and data:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    return data


# ── TOC parsing ───────────────────────────────────────────────────────────────

def parse_toc(html: str) -> list[dict]:
    """Extract ordered TOC from main page HTML (Next.js RSC payload)."""
    toc_idx = html.find('\\"content\\":[{\\"title\\":\\"1.')
    if toc_idx == -1:
        raise RuntimeError("Структура TOC не найдена в HTML главной страницы")
    raw = html[toc_idx:toc_idx + 30000].replace('\\"', '"').replace('\\\\', '\\')
    bracket = raw[10:]  # skip '"content":'
    depth = 0
    for i, ch in enumerate(bracket):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                return json.loads(bracket[:i + 1])
    raise RuntimeError("Не удалось распарсить TOC")


def toc_hash(toc: list[dict]) -> str:
    slugs = [a["slug"] for ch in toc for a in ch["articles"]]
    return hashlib.sha256("|".join(slugs).encode()).hexdigest()[:16]


# ── Content extraction ────────────────────────────────────────────────────────

def extract_article_html(page_html: str) -> str:
    """Return inner HTML of #wysiwyg-client-content."""
    marker = 'id="wysiwyg-client-content"'
    idx = page_html.find(marker)
    if idx == -1:
        return "<p><em>Контент не найден.</em></p>"
    text = page_html[idx:]
    tag_end = text.index('>') + 1
    i = tag_end
    depth = 1
    while i < len(text) - 5:
        if text[i:i+4] == '<div':
            depth += 1
            i += 4
        elif text[i:i+5] == '</div':
            depth -= 1
            if depth == 0:
                return text[tag_end:i]
            i += 5
        else:
            i += 1
    return text[tag_end:]


# ── Math rendering ────────────────────────────────────────────────────────────

def _render_math(m: re.Match) -> str:
    latex = unquote(m.group(1))
    try:
        opts = json.loads(unquote(m.group(2) or "{}"))
        display = "block" if opts.get("displayMode", False) else "inline"
    except Exception:
        display = "inline"
    try:
        return latex2mathml.converter.convert(latex, display=display)
    except Exception:
        tag = "div" if display == "block" else "span"
        return f'<{tag} class="math-src">${"$" if display=="block" else ""}{latex}{"$$" if display=="block" else "$"}</{tag}>'


_MATH_RE = re.compile(
    r'<span[^>]*class="[^"]*yfm-latex[^"]*"[^>]*'
    r'data-content="([^"]*)"[^>]*'
    r'data-options="([^"]*)"[^>]*>\s*</span>',
    re.DOTALL,
)


def process_math(html: str) -> str:
    return _MATH_RE.sub(_render_math, html)


# ── Code block decoding ───────────────────────────────────────────────────────

_PRE_RE = re.compile(
    r'<pre([^>]*)\bclass="([^"]*pre-code-lines[^"]*)"([^>]*)'
    r'\bdata-content="([^"]*)"([^>]*)>(.*?)</pre>',
    re.DOTALL,
)

_LINE_NUM_STRIP = re.compile(r'^\s*\d+\s*', re.MULTILINE)


def _replace_code_block(m: re.Match) -> str:
    encoded = m.group(4)
    code_raw = unquote(encoded)
    code_html = (
        code_raw
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f'<pre class="code-block"><code>{code_html}</code></pre>'


def process_code_blocks(html: str) -> str:
    return _PRE_RE.sub(_replace_code_block, html)


# ── Image caching ─────────────────────────────────────────────────────────────

def _replace_img_src(m: re.Match) -> str:
    src = m.group(1)
    if src.startswith("data:"):
        return m.group(0)
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("http"):
        return m.group(0)
    img_hash = hashlib.md5(src.encode()).hexdigest()[:16]
    clean_path = src.split("?")[0].split("/")[-1]
    raw_ext = clean_path.rsplit(".", 1)
    ext = re.sub(r'[^a-zA-Z0-9]', '', raw_ext[-1])[:8] if len(raw_ext) > 1 else "img"
    ext = ext or "img"
    img_path = IMG_CACHE / f"{img_hash}.{ext}"
    if not img_path.exists():
        data = fetch(src)
        if data:
            img_path.write_bytes(data)
    if img_path.exists():
        return f'src="{img_path.resolve()}"'
    return m.group(0)


def process_images(html: str) -> str:
    return re.sub(r'src="([^"]+)"', _replace_img_src, html)


# ── Details (yfm-cut) ─────────────────────────────────────────────────────────

def process_details(html: str) -> str:
    """Force all <details> open so yfm-cut proofs/asides show in PDF."""
    return re.sub(r'<details\b(?![^>]*\bopen\b)', '<details open', html)


# ── HTML assembly ─────────────────────────────────────────────────────────────

def make_css() -> str:
    f = font_url

    return f"""\
/* ── Fonts ── */
@font-face {{
    font-family: "CoFoSans";
    font-weight: 300;
    src: url("{f('f2f0493f5123f937-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "CoFoSans";
    font-weight: 400;
    font-style: normal;
    src: url("{f('a853c69d3cf13b17-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "CoFoSans";
    font-weight: 400;
    font-style: italic;
    src: url("{f('f3f9c83d0bcb2176-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "CoFoSans";
    font-weight: 500;
    src: url("{f('b4b0da158404816f-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "CoFoSans";
    font-weight: 700;
    src: url("{f('e10f0a1f1c5bddfe-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "CoFoSansMono";
    font-weight: 400;
    src: url("{f('c8ae0fac15b37b16-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "YSText";
    font-weight: 300;
    src: url("{f('305b936a915bc48f-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "YSText";
    font-weight: 400;
    font-style: normal;
    src: url("{f('3fdc59da94114ecd-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "YSText";
    font-weight: 400;
    font-style: italic;
    src: url("{f('0f6801932ea3fcf4-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "YSText";
    font-weight: 500;
    src: url("{f('dd32e121f6104240-s.p.woff2')}") format("woff2");
}}
@font-face {{
    font-family: "YSText";
    font-weight: 700;
    src: url("{f('cc87cb16fedd6384-s.p.woff2')}") format("woff2");
}}

/* ── Page layout + running elements ── */
@page {{
    size: A4;
    margin: 2.5cm 2.2cm 3cm;
    @bottom-center {{
        content: counter(page);
        font-family: "CoFoSans", sans-serif;
        font-size: 8.5pt;
        color: #c0c0c0;
        margin-top: 0.4cm;
    }}
    @top-right {{
        content: string(chapter-title, last);
        font-family: "CoFoSans", sans-serif;
        font-size: 7.5pt;
        color: #b0b0b0;
        vertical-align: bottom;
        padding-bottom: 4pt;
        border-bottom: 0.5pt solid #e8e8e8;
    }}
}}
/* ── Named page for cover: zero margins = full A4 canvas ── */
@page cover-page {{
    size: A4;
    margin: 0;
    @bottom-center {{ content: ""; }}
    @top-right     {{ content: ""; }}
}}

/* ── Base ── */
body {{
    font-family: "YSText", Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.65;
    color: #323232;
    hyphens: auto;
}}

p {{
    margin: 0.6em 0;
    text-align: justify;
    orphans: 3;
    widows: 3;
}}

/* ── Cover ── */
.cover {{
    page: cover-page;
    page-break-after: always;
    background: #F5EB7D;
    height: 29.7cm;
    box-sizing: border-box;
    padding: 9cm 2.5cm 0;
    position: relative;
}}
/* Reset global p styles inside cover */
.cover p {{
    margin: 0;
    text-align: left;
    orphans: 1;
    widows: 1;
}}
.cover-org {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 500;
    font-size: 8pt;
    color: #555;
    text-transform: uppercase;
    letter-spacing: 0.13em;
    margin-bottom: 0.9em;
}}
.cover-bar {{
    background: #FF6E55;
    height: 0.45cm;
    margin-bottom: 1em;
}}
.cover-title {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 38pt;
    color: #1C1C1C;
    line-height: 1.05;
    margin-bottom: 0.9em;
    letter-spacing: -0.01em;
}}
.cover-rule {{
    border: none;
    border-top: 1.5px solid #999;
    margin: 0 0 0.75em;
}}
.cover-stats {{
    font-family: "CoFoSans", sans-serif;
    font-size: 10pt;
    color: #444;
    margin-bottom: 0.25em;
}}
.cover-date {{
    font-family: "CoFoSans", sans-serif;
    font-size: 9pt;
    color: #777;
}}
.cover-attribution {{
    position: absolute;
    bottom: 2.2cm;
    left: 2.5cm;
    right: 2.5cm;
    font-family: "YSText", sans-serif;
    font-size: 7.5pt;
    color: #666;
    line-height: 1.6;
    border-top: 1px solid #bbb;
    padding-top: 0.6em;
    text-align: left;
}}

/* ── TOC ── */
.toc {{
    page-break-after: always;
    padding-top: 0.5em;
}}
.toc > h1 {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 22pt;
    color: #1a1a1a;
    border-bottom: 3px solid #FF6E55;
    padding-bottom: 0.35em;
    margin-bottom: 1.3em;
    margin-top: 0;
}}
.toc ul  {{ list-style: none; padding: 0; margin: 0; }}
.toc li.ch {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 10.5pt;
    color: #1a1a1a;
    margin-top: 1.1em;
    padding-top: 0.45em;
    border-top: 1px solid #ebebeb;
}}
.toc li.ar {{
    font-family: "YSText", sans-serif;
    font-size: 9.5pt;
    line-height: 1.55;
    margin-left: 1.6em;
    color: #555;
    padding: 0.08em 0;
}}
.toc li.ar .num {{
    color: #b0b0b0;
    width: 2.8em;
    display: inline-block;
    font-size: 9pt;
}}

/* ── Chapter page ── */
.chapter-page {{
    page-break-before: always;
    margin-bottom: 2em;
}}
.chapter-kicker {{
    font-family: "CoFoSans", sans-serif;
    font-size: 8.5pt;
    font-weight: 500;
    color: #FF6E55;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    margin: 0 0 0.6em;
}}
h1.chapter-header {{
    string-set: chapter-title content(text);
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 24pt;
    color: #1a1a1a;
    line-height: 1.15;
    margin: 0;
    padding-bottom: 0.5em;
    border-bottom: 3px solid #FF6E55;
}}

/* ── Article header ── */
.article-header {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 15pt;
    color: #1a1a1a;
    margin-top: 2.5em;
    margin-bottom: 0.2em;
    page-break-after: avoid;
    line-height: 1.25;
    border-bottom: 1px solid #ebebeb;
    padding-bottom: 0.4em;
}}
.article-header .num {{
    color: #c0c0c0;
    font-weight: 400;
    font-size: 11pt;
    margin-right: 0.35em;
}}

/* ── Content headings ── */
h2 {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 13pt;
    color: #1a1a1a;
    margin-top: 1.8em;
    margin-bottom: 0.5em;
    page-break-after: avoid;
    line-height: 1.3;
}}
h3 {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 500;
    font-size: 11.5pt;
    color: #323232;
    background: #F0F1F2;
    padding: 0.2em 0.6em;
    margin-top: 1.4em;
    margin-bottom: 0.5em;
    page-break-after: avoid;
}}
h4 {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 500;
    font-size: 10.5pt;
    color: #323232;
    margin-top: 1.2em;
    margin-bottom: 0.4em;
    page-break-after: avoid;
}}

/* ── Code ── */
code {{
    font-family: "CoFoSansMono", "Courier New", monospace;
    font-size: 8.5pt;
    background: #f5f1f5;
    color: #5c3d5c;
    padding: 0.12em 0.35em;
    border-radius: 2px;
}}
pre.code-block {{
    background: #f9f7f9;
    border-left: 3px solid #FF6E55;
    border-top: 1px solid #ece8ec;
    border-bottom: 1px solid #ece8ec;
    padding: 0.85em 1.1em;
    margin: 1.1em 0;
    font-size: 8pt;
    line-height: 1.55;
    page-break-inside: avoid;
    white-space: pre-wrap;
    word-break: break-all;
}}
pre.code-block code {{
    background: none;
    color: #5c3d5c;
    padding: 0;
    font-size: inherit;
    border-radius: 0;
}}
/* line numbers from server-side rendering — hide them */
span.line-number {{ display: none; }}

/* ── Math ── */
math               {{ font-size: 1.05em; }}
math[display="block"] {{ display: block; margin: 0.9em auto; text-align: center; }}
.math-src          {{ font-family: "CoFoSansMono", monospace; font-size: 9pt; color: #695d69; }}

/* ── Images ── */
img {{
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1.3em auto;
    page-break-inside: avoid;
}}
figure        {{ margin: 1.5em 0; text-align: center; }}
figcaption    {{ font-size: 9pt; color: #888; margin-top: 0.4em; font-style: italic; }}
.fig-img img  {{ display: block; margin: 0 auto; }}

/* ── Tables ── */
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 1.3em 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
}}
th {{
    background: #1a1a1a;
    color: #fff;
    font-family: "CoFoSans", sans-serif;
    font-weight: 500;
    text-align: left;
    font-size: 9pt;
}}
th, td {{
    border: 1px solid #c8c8c8;
    padding: 0.4em 0.7em;
    vertical-align: top;
}}
tr:nth-child(even) td {{ background: #fafafa; }}

/* ── Lists ── */
ul, ol {{ padding-left: 1.8em; margin: 0.5em 0; }}
li     {{ margin-bottom: 0.3em; line-height: 1.6; }}

/* ── Blockquote ── */
blockquote {{
    border-left: 3px solid #873CF5;
    margin: 1.3em 0;
    padding: 0.5em 1em;
    color: #4a4a4a;
    background: #f8f6ff;
    font-style: italic;
}}

/* ── yfm-cut (collapsible proofs / asides) ── */
details.yfm-cut {{
    border: 1px solid #e0dce8;
    border-left: 3px solid #873CF5;
    border-radius: 2px;
    margin: 1.3em 0;
    padding: 0.8em 1.1em 1em;
    background: #faf8ff;
    page-break-inside: avoid;
}}
summary.yfm-cut-title {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 500;
    font-size: 10pt;
    color: #873CF5;
    list-style: none;
    margin-bottom: 0.7em;
    padding-bottom: 0.5em;
    border-bottom: 1px solid #ede8ff;
}}
summary.yfm-cut-title::before {{
    content: "▸ ";
    font-size: 9pt;
}}
.yfm-cut-content {{ font-size: 10pt; }}

/* ── Colophon (last page) ── */
.colophon {{
    page-break-before: always;
    page-break-inside: avoid;
    padding-top: 7cm;
}}
.colophon-title {{
    font-family: "CoFoSans", sans-serif;
    font-weight: 700;
    font-size: 13pt;
    color: #1a1a1a;
    margin: 0 0 1.4em;
    padding-bottom: 0.5em;
    border-bottom: 2px solid #FF6E55;
}}
.colophon-block {{
    margin-bottom: 1em;
}}
.colophon-label {{
    font-family: "CoFoSans", sans-serif;
    font-size: 7.5pt;
    font-weight: 500;
    color: #b0b0b0;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 0.2em;
}}
.colophon-value {{
    font-family: "YSText", sans-serif;
    font-size: 10pt;
    color: #323232;
    line-height: 1.5;
}}
.colophon-code {{
    font-family: "CoFoSansMono", monospace;
    font-size: 9pt;
    background: none;
    color: #695d69;
    padding: 0;
}}
.colophon-notice {{
    margin-top: 1.8em;
    padding-top: 0.9em;
    border-top: 1px solid #e8e8e8;
    font-family: "YSText", sans-serif;
    font-size: 8.5pt;
    color: #999;
    line-height: 1.6;
}}

/* ── Page-break hygiene ── */

/* Headings must not be orphaned at the bottom of a page */
h1, h2, h3, h4,
.chapter-header, .chapter-kicker,
.article-header {{
    page-break-after: avoid;
    page-break-inside: avoid;
}}

/* Element right after a heading must not start a new page */
h2 + p, h2 + ul, h2 + ol, h2 + pre, h2 + table, h2 + figure, h2 + div,
h3 + p, h3 + ul, h3 + ol, h3 + pre, h3 + table, h3 + figure, h3 + div,
h4 + p, h4 + ul, h4 + ol, h4 + pre, h4 + table, h4 + figure, h4 + div,
.article-header + p, .article-header + ul, .article-header + div {{
    page-break-before: avoid;
}}

/* Chapter and article structure stays together */
.chapter-page  {{ page-break-inside: avoid; }}

/* Floated / self-contained blocks */
figure, table, pre.code-block, details.yfm-cut {{ page-break-inside: avoid; }}

/* List items with sub-content */
li {{ page-break-inside: avoid; }}

/* Paragraphs: at least 3 lines on each side of a page break */
p, li {{ orphans: 3; widows: 3; }}

/* ── Other ── */
a      {{ color: #873CF5; text-decoration: none; }}
hr     {{ border: none; border-top: 1px solid #e0e0e0; margin: 1.5em 0; }}
strong {{ font-weight: 700; }}
em     {{ font-style: italic; }}
sup, sub {{ font-size: 0.75em; }}
"""


def toc_to_html(toc: list[dict]) -> str:
    lines = ['<div class="toc"><h1>Содержание</h1><ul>']
    for ch in toc:
        lines.append(f'<li class="ch">{ch["title"]}</li>')
        for art in ch["articles"]:
            lines.append(
                f'<li class="ar"><span class="num">{art["articleNumber"]}</span>'
                f'{art["title"]}</li>'
            )
    lines.append("</ul></div>")
    return "\n".join(lines)


def colophon_html(build_hash: str = "") -> str:
    date = time.strftime("%d.%m.%Y")
    hash_block = (
        f'<div class="colophon-block">'
        f'<div class="colophon-label">Хэш TOC</div>'
        f'<div class="colophon-value"><code class="colophon-code">{build_hash}</code></div>'
        f'</div>'
    ) if build_hash else ""
    return (
        '<div class="colophon">'

        '<p class="colophon-title">Учебник по машинному обучению — Яндекс ШАД</p>'

        '<div class="colophon-block">'
        '<div class="colophon-label">Авторы учебных материалов</div>'
        '<div class="colophon-value">Яндекс · Школа анализа данных<br>'
        'education.yandex.ru/handbook/ml</div>'
        '</div>'

        '<div class="colophon-block">'
        '<div class="colophon-label">Идея, парсинг и PDF-вёрстка</div>'
        '<div class="colophon-value">lakomoor<br>'
        'github.com/lakomoor/mlbook</div>'
        '</div>'

        '<div class="colophon-block">'
        '<div class="colophon-label">Технический стек</div>'
        '<div class="colophon-value">'
        'Python 3 · WeasyPrint · latex2mathml<br>'
        'Шрифты: CoFoSans · YSText · CoFoSansMono'
        '</div>'
        '</div>'

        f'<div class="colophon-block">'
        f'<div class="colophon-label">Дата сборки</div>'
        f'<div class="colophon-value">{date}</div>'
        f'</div>'

        f'{hash_block}'

        '<div class="colophon-notice">'
        '© ООО «Яндекс». Все права на учебные материалы принадлежат их авторам и ООО «Яндекс».<br>'
        'Данный документ создан для личного некоммерческого использования '
        'и не предназначен для распространения.'
        '</div>'

        '</div>'
    )


def cover_html(total_chapters: int = 16, total_articles: int = 72) -> str:
    date = time.strftime("%d.%m.%Y")
    return (
        '<div class="cover">'
        '<p class="cover-org">Яндекс · Школа анализа данных</p>'
        '<div class="cover-bar"></div>'
        '<p class="cover-title">Учебник по<br>машинному<br>обучению</p>'
        '<hr class="cover-rule">'
        f'<p class="cover-stats">{total_chapters} глав · {total_articles} статей</p>'
        f'<p class="cover-date">Сгенерировано {date}</p>'
        '<p class="cover-attribution">'
        '© ООО «Яндекс». Все права на материалы принадлежат их авторам и Яндексу.<br>'
        'education.yandex.ru/handbook/ml · Создано для личного некоммерческого использования'
        '</p>'
        '</div>'
    )


# ── Main build ────────────────────────────────────────────────────────────────

def build(force: bool = False) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    PAGE_CACHE.mkdir(exist_ok=True)
    IMG_CACHE.mkdir(exist_ok=True)

    print("📖 Яндекс ML Handbook → PDF\n")

    print("▶ Загружаем оглавление...")
    main_html = fetch(
        f"{BASE_URL}/", CACHE_DIR / "main.html", force=force
    ).decode("utf-8", errors="replace")
    toc = parse_toc(main_html)
    total_arts = sum(len(ch["articles"]) for ch in toc)
    print(f"  {len(toc)} глав, {total_arts} статей\n")

    parts = [cover_html(len(toc), total_arts), toc_to_html(toc)]
    done = 0

    for ch in toc:
        ch_num = ch["title"].split(".")[0] if "." in ch["title"] else ""
        ch_kicker = f'<p class="chapter-kicker">Глава {ch_num}</p>' if ch_num else ""
        parts.append(
            f'<div class="chapter-page">'
            f'{ch_kicker}'
            f'<h1 class="chapter-header">{ch["title"]}</h1>'
            f'</div>'
        )

        for art in ch["articles"]:
            done += 1
            slug = art["slug"]
            num = art["articleNumber"]
            title = art["title"]
            print(f"  [{done:2d}/{total_arts}] {num}. {title}")

            url = f"{BASE_URL}/article/{slug}"
            cache = PAGE_CACHE / f"{slug}.html"
            page_html = fetch(url, cache, force=force).decode("utf-8", errors="replace")

            raw_content = extract_article_html(page_html)
            content = process_code_blocks(raw_content)
            content = process_details(content)
            content = process_math(content)
            content = process_images(content)

            parts.append(
                f'<div class="article-header">'
                f'<span class="num">{num}</span>{title}</div>'
            )
            parts.append(content)
            time.sleep(0.25)

    build_hash = (CACHE_DIR / "toc.hash").read_text().strip() if (CACHE_DIR / "toc.hash").exists() else ""
    parts.append(colophon_html(build_hash))

    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Учебник по машинному обучению — Яндекс</title>
<style>{make_css()}</style>
</head>
<body>
{"".join(parts)}
</body>
</html>"""

    OUTPUT_HTML.write_text(full_html, encoding="utf-8")
    print(f"\n✔ HTML: {OUTPUT_HTML} ({OUTPUT_HTML.stat().st_size // 1024} KB)")

    print("⏳ Генерируем PDF (может занять пару минут)...")
    try:
        doc = weasyprint.HTML(filename=str(OUTPUT_HTML)).write_pdf()
    except Exception as e:
        # fontTools может падать при сабсеттинге шрифтов с битыми MATH-таблицами;
        # hinting=False пропускает проблемный путь компиляции.
        print(f"⚠ Ошибка генерации (retry hinting=False): {e}")
        doc = weasyprint.HTML(filename=str(OUTPUT_HTML)).write_pdf(hinting=False)
    OUTPUT_PDF.write_bytes(doc)
    size_mb = OUTPUT_PDF.stat().st_size / 1024 / 1024
    print(f"✔ PDF:  {OUTPUT_PDF} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    force = "--force" in sys.argv or "-f" in sys.argv
    build(force=force)
