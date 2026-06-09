# mlbook

Скачивает [Учебник по машинному обучению Яндекс ШАД](https://education.yandex.ru/handbook/ml) и собирает из него PDF-книгу с нормальной вёрсткой — шрифты как на сайте, математика, картинки, номера страниц.

---

## Зачем

Официальный PDF учебника не существует. Учебник живёт только на сайте, без оффлайн-доступа. Этот проект парсит его и верстает в PDF, который можно читать в метро, на планшете или распечатать.

## Что внутри

- **16 глав, 72 статьи** — весь учебник целиком
- **Родные шрифты Яндекса** — CoFoSans, YSText, CoFoSansMono
- **Математика** — LaTeX → MathML через `latex2mathml`
- **Код** — URL-encoded `data-content` блоки декодируются в читаемый текст
- **Раскрытые доказательства** — `<details class="yfm-cut">` разворачиваются в PDF
- **Встроенные изображения** — скачиваются и кешируются локально
- **Номера страниц** + колонтитул с названием текущей главы
- **Обложка** — в цветах Яндекса, с атрибуцией
- **Колофон** — последняя страница с мета-информацией о сборке

## Как запустить

```bash
# 1. Зависимости
pip install uv
uv venv && source .venv/bin/activate
uv pip install weasyprint latex2mathml

# macOS: нужен pango через Homebrew
brew install pango

# 2. Сборка PDF
python build_pdf.py

# Принудительно перескачать все страницы
python build_pdf.py --force
```

Результат: `ml_handbook.pdf` (~200 MB).

## Кеш

Страницы и изображения кешируются в `cache/` — повторная сборка занимает пару минут вместо получаса.

```
cache/
  pages/      # HTML каждой статьи
  images/     # скачанные картинки
  fonts/      # шрифты Яндекса
  main.html   # главная страница с TOC
  toc.hash    # хэш оглавления для детектирования изменений
```

## Автоматическое обновление (GitHub Actions)

Workflow запускается каждый понедельник в 08:00 UTC и при изменении оглавления публикует новый PDF как [GitHub Release](../../releases).

```
.github/workflows/build_handbook.yml
```

Ручной запуск: **Actions → Build ML Handbook PDF → Run workflow**.

## Как работает парсинг

Сайт построен на Next.js с RSC (React Server Components). TOC и контент статей зашиты прямо в HTML как экранированный JSON-payload — никакого API не нужно.

```
HTML страницы
  └─ \"content\":[{\"title\":\"1. ...  ← escaped JSON с оглавлением
       └─ json.loads() → список глав и статей

HTML статьи
  └─ #wysiwyg-client-content       ← контент статьи
       ├─ <span class="yfm-latex" data-content="...">  ← URL-encoded LaTeX
       ├─ <pre class="pre-code-lines" data-content="...">  ← URL-encoded код
       └─ <details class="yfm-cut">  ← доказательства/детали
```

## Стек

| Компонент | Версия |
|---|---|
| Python | 3.13 |
| WeasyPrint | 69 |
| latex2mathml | последняя |

## Disclaimer

Все права на учебные материалы принадлежат их авторам и ООО «Яндекс».  
Репозиторий создан для личного некоммерческого использования.  
Не распространяйте скомпилированный PDF публично.
