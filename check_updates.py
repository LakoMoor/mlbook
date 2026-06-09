#!/usr/bin/env python3
"""
Проверяет обновления Яндекс ML Handbook и пересобирает PDF при изменениях.

Usage:
    python check_updates.py            # тихая проверка
    python check_updates.py --verbose  # с подробным выводом
    python check_updates.py --force    # пересобрать без проверки

Для cron (пример — каждый день в 8:00):
    0 8 * * * /Users/lakomoor/mlbook/run_check.sh >> /Users/lakomoor/mlbook/logs/updates.log 2>&1
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_URL = "https://education.yandex.ru/handbook/ml"
CACHE_DIR = Path("cache")
HASH_FILE = CACHE_DIR / "toc.hash"
LOG_FILE = Path("logs") / "updates.log"
SCRIPT_DIR = Path(__file__).parent


def fetch_main_page() -> str:
    result = subprocess.run(
        ["curl", "-skL", "--max-time", "30", f"{BASE_URL}/"],
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def extract_toc_raw(html: str) -> str:
    """Return raw escaped TOC string for hashing."""
    start = html.find('\\"content\\":[{\\"title\\":\\"1.')
    if start == -1:
        return ""
    # The content array ends at the RSC push boundary — take a fixed window
    return html[start:start + 20000]


def extract_toc(html: str) -> list[dict]:
    raw = extract_toc_raw(html).replace('\\"', '"').replace('\\\\', '\\')
    bracket = raw[10:]
    depth = 0
    for i, ch in enumerate(bracket):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                return json.loads(bracket[:i + 1])
    return []


def toc_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def notify(message: str) -> None:
    """Send macOS notification."""
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{message}" with title "ML Handbook"'],
        capture_output=True,
    )


def run_build(force: bool = False) -> bool:
    cmd = [sys.executable, str(SCRIPT_DIR / "build_pdf.py")]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    return result.returncode == 0


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    force = "--force" in sys.argv or "-f" in sys.argv

    CACHE_DIR.mkdir(exist_ok=True)
    LOG_FILE.parent.mkdir(exist_ok=True)

    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    if force:
        print(f"[{ts}] Принудительная пересборка...")
        shutil.rmtree(CACHE_DIR / "pages", ignore_errors=True)
        (CACHE_DIR / "main.html").unlink(missing_ok=True)
        ok = run_build(force=True)
        if ok:
            print(f"[{ts}] ✔ PDF пересобран")
            notify("ML Handbook PDF обновлён")
            # Update stored hash
            html = fetch_main_page()
            raw = extract_toc_raw(html)
            if raw:
                HASH_FILE.write_text(toc_hash(raw))
        else:
            print(f"[{ts}] ✗ Ошибка сборки")
        return 0 if ok else 1

    print(f"[{ts}] Проверяем обновления ML Handbook...")
    html = fetch_main_page()
    raw = extract_toc_raw(html)
    if not raw:
        print(f"[{ts}] ⚠ Не удалось получить TOC (сайт недоступен?)")
        return 1

    current = toc_hash(raw)
    stored = HASH_FILE.read_text().strip() if HASH_FILE.exists() else None

    if verbose:
        toc = extract_toc(html)
        total = sum(len(ch["articles"]) for ch in toc)
        print(f"  Глав: {len(toc)}, статей: {total}")
        print(f"  Текущий хеш: {current}")
        print(f"  Сохранённый: {stored or '(нет)'}")

    if current == stored:
        print(f"[{ts}] ✔ Изменений нет (хеш: {current})")
        return 0

    print(f"[{ts}] ★ Обнаружены изменения! {stored or '(первый запуск)'} → {current}")

    # Clear stale cache and rebuild
    shutil.rmtree(CACHE_DIR / "pages", ignore_errors=True)
    (CACHE_DIR / "main.html").unlink(missing_ok=True)

    ok = run_build()
    if ok:
        HASH_FILE.write_text(current)
        print(f"[{ts}] ✔ PDF пересобран, хеш обновлён: {current}")
        notify("ML Handbook обновлён — PDF пересобран")
    else:
        print(f"[{ts}] ✗ Ошибка при сборке PDF")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
