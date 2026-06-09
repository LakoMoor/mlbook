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
OUTPUT_PDF = Path("ml_handbook.pdf")
OUTPUT_HTML = Path("ml_handbook.html")


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
    # Unescape the section starting from "content":[
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


# ── HTML assembly ─────────────────────────────────────────────────────────────

BOOK_CSS = """\
@page {
    size: A4;
    margin: 2.5cm 2cm 3cm;
}
@page :left  { margin-left: 2.5cm; margin-right: 1.8cm; }
@page :right { margin-left: 1.8cm; margin-right: 2.5cm; }

body {
    font-family: Georgia, "DejaVu Serif", "Times New Roman", serif;
    font-size: 10.5pt;
    line-height: 1.65;
    color: #1a1a1a;
    text-align: justify;
    hyphens: auto;
}

/* ── Cover ── */
.cover {
    page-break-after: always;
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 24cm;
    text-align: center;
}
.cover h1   { font-size: 26pt; color: #c0392b; margin-bottom: 0.3em; }
.cover .sub { font-size: 13pt; color: #555; }
.cover .gen { font-size: 9.5pt; color: #aaa; margin-top: 1em; }

/* ── TOC ── */
.toc { page-break-after: always; }
.toc > h1 { font-size: 20pt; border-bottom: 2px solid #c0392b; padding-bottom: .3em; }
.toc ul { list-style: none; padding: 0; }
.toc li.ch { font-weight: bold; font-size: 11pt; margin-top: .7em; color: #2c3e50; }
.toc li.ar { margin-left: 1.8em; font-size: 10pt; line-height: 1.5; }
.toc li.ar .num { color: #999; width: 3em; display: inline-block; }

/* ── Chapter / Article headings ── */
.chapter-header {
    page-break-before: always;
    font-size: 19pt;
    font-weight: bold;
    color: #c0392b;
    border-bottom: 2.5px solid #c0392b;
    padding-bottom: .4em;
    margin-bottom: 1.4em;
    margin-top: 0;
}
.article-header {
    font-size: 14pt;
    font-weight: bold;
    color: #2c3e50;
    margin-top: 2.5em;
    margin-bottom: .6em;
    page-break-after: avoid;
}
.article-header .num { color: #999; font-weight: normal; font-size: 11pt; }

/* ── Content headings ── */
h2 { font-size: 12.5pt; color: #2c3e50; margin-top: 1.6em; page-break-after: avoid; }
h3 { font-size: 11.5pt; color: #34495e; margin-top: 1.3em; page-break-after: avoid; }
h4 { font-size: 11pt; margin-top: 1.1em; page-break-after: avoid; }

/* ── Code ── */
code {
    font-family: "Courier New", Courier, monospace;
    font-size: 8.5pt;
    background: #f5f5f5;
    padding: .1em .35em;
    border-radius: 3px;
    border: 1px solid #e0e0e0;
}
pre {
    background: #f7f7f7;
    border-left: 3px solid #c0392b;
    padding: .7em 1em;
    margin: 1em 0;
    font-size: 8pt;
    line-height: 1.4;
    page-break-inside: avoid;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
}
pre code { background: none; border: none; padding: 0; font-size: inherit; }

/* ── Math ── */
math          { font-size: 1.05em; }
math[display="block"] { display: block; margin: .8em auto; text-align: center; }
.math-src     { font-family: monospace; font-size: 9pt; color: #555; }

/* ── Images ── */
img { max-width: 100%; height: auto; display: block; margin: 1em auto; page-break-inside: avoid; }
figure { margin: 1.2em 0; text-align: center; }
figcaption { font-size: 9pt; color: #666; margin-top: .3em; }

/* ── Tables ── */
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 9.5pt; page-break-inside: avoid; }
th    { background: #f0f0f0; font-weight: bold; }
th, td { border: 1px solid #ccc; padding: .35em .6em; }

/* ── Other ── */
blockquote { border-left: 3px solid #c0392b; margin: 1em 0; padding: .4em 1em; color: #555; background: #fafafa; }
a          { color: #2980b9; text-decoration: none; }
hr         { border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }
ul, ol     { padding-left: 1.6em; }
li         { margin-bottom: .2em; }
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


def cover_html() -> str:
    date = time.strftime("%d.%m.%Y")
    return (
        '<div class="cover">'
        "<h1>Учебник по машинному обучению</h1>"
        '<p class="sub">Яндекс · education.yandex.ru/handbook/ml</p>'
        f'<p class="gen">Сгенерировано {date}</p>'
        "</div>"
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

    parts = [cover_html(), toc_to_html(toc)]
    done = 0

    for ch in toc:
        parts.append(f'<h1 class="chapter-header">{ch["title"]}</h1>')

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
            content = process_math(raw_content)
            content = process_images(content)

            parts.append(
                f'<div class="article-header">'
                f'<span class="num">{num} &nbsp;</span>{title}</div>'
            )
            parts.append(content)
            time.sleep(0.25)

    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Учебник по машинному обучению — Яндекс</title>
<style>{BOOK_CSS}</style>
</head>
<body>
{"".join(parts)}
</body>
</html>"""

    OUTPUT_HTML.write_text(full_html, encoding="utf-8")
    print(f"\n✔ HTML: {OUTPUT_HTML} ({OUTPUT_HTML.stat().st_size // 1024} KB)")

    print("⏳ Генерируем PDF (может занять пару минут)...")
    doc = weasyprint.HTML(filename=str(OUTPUT_HTML)).write_pdf()
    OUTPUT_PDF.write_bytes(doc)
    size_mb = OUTPUT_PDF.stat().st_size / 1024 / 1024
    print(f"✔ PDF:  {OUTPUT_PDF} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    force = "--force" in sys.argv or "-f" in sys.argv
    build(force=force)
