#!/usr/bin/env python3
"""
Экспорт приложения в PDF и проверка объёма (шаблон чемпионата: 10–15 страниц).

    pip install markdown weasyprint          # один раз
    # без WeasyPrint: pip install playwright pymupdf && playwright install chromium (печать через Chromium)
    python3 docs/appendix/make_pdf.py        # submission/appendix.md -> .html + .pdf + число страниц

    python3 docs/appendix/make_pdf.py --max-pages 15 --min-pages 10
    python3 docs/appendix/make_pdf.py --input other.md --output other.pdf

Код выхода 1, если страниц больше --max-pages. Если WeasyPrint не установлен или не запускается
(на Windows ему нужен GTK), скрипт всё равно пишет appendix.html: откройте его в браузере,
Ctrl+P → «Сохранить как PDF», размер A4, поля «обычные», включите «Фон»; число страниц смотрите в предпросмотре.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm;
        @bottom-center { content: counter(page) " / " counter(pages); font-size: 8pt; color: #666; } }
html { font-family: Inter, 'Segoe UI', 'DejaVu Sans', Arial, sans-serif; font-size: 9pt; line-height: 1.35; color: #1b2430; }
h1 { font-size: 18pt; margin: 0 0 6mm; }
h2 { font-size: 13pt; margin: 7mm 0 2mm; border-bottom: 1px solid #c9ced6; padding-bottom: 1mm; }
h3 { font-size: 11pt; margin: 5mm 0 1.5mm; }
h1, h2, h3, h4 { break-after: avoid; }
p { margin: 1.5mm 0; }
table { border-collapse: collapse; width: 100%; margin: 2mm 0 3mm; font-size: 7.6pt; }
th, td { border: 1px solid #b8bfc9; padding: 1mm 1.4mm; vertical-align: top; overflow-wrap: anywhere; }
th { background: #eef1f4; text-align: left; }
tr { break-inside: avoid; }
thead { display: table-header-group; }
img { max-width: 100%; max-height: 215mm; display: block; margin: 2mm auto; break-inside: avoid; }
code { font-family: 'DejaVu Sans Mono', Consolas, monospace; font-size: 0.9em; background: #f1f3f5; padding: 0 0.6mm; }
pre { background: #f1f3f5; padding: 1.5mm 2mm; white-space: pre-wrap; break-inside: avoid; }
pre code { background: none; padding: 0; }
blockquote { margin: 2mm 0; padding: 0.5mm 3mm; border-left: 3px solid #c9ced6; color: #4a5568; }
ul, ol { margin: 1mm 0; padding-left: 5mm; }
"""


JOURNAL_WIDTHS = [4, 9, 12, 24, 19, 18, 14]  # %: узкие колонки журнала ИИ не должны ломать слова


def set_journal_widths(html):
    """Таблице журнала ИИ (первая колонка «№») задаём ширины колонок, остальные таблицы остаются автоматическими."""
    start = html.find("<th>№</th>")
    if start == -1:
        return html
    end = html.find("</tr>", start)
    row, widths = html[start:end], iter(JOURNAL_WIDTHS)
    row = re.sub(r"<th>", lambda m: f'<th style="width:{next(widths, 10)}%">', row)
    return html[:start] + row + html[end:]


def set_area_widths(html):
    """Первая колонка таблицы «Продуктовая реализация» (A.5) узкая по содержимому: задаём ширину, чтобы слова не рвались."""
    return html.replace("<th>Область</th>", '<th style="width:16%">Область</th>')


def print_with_chromium(html_path, pdf_path):
    """Запасной путь без WeasyPrint: печать HTML в PDF через Chromium (pip install playwright; playwright install chromium).
    Возвращает число страниц или None, если Playwright или браузер недоступны."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("file://" + os.path.abspath(html_path))
            page.wait_for_load_state("networkidle")
            page.pdf(path=pdf_path, prefer_css_page_size=True, print_background=True)
            browser.close()
    except Exception as exc:  # браузер не скачан или не запустился
        print(f"Chromium недоступен ({type(exc).__name__}: {exc})")
        return None
    try:
        import fitz  # PyMuPDF, только чтобы посчитать страницы
        return len(fitz.open(pdf_path))
    except ImportError:
        return 0


def parse_args():
    ap = argparse.ArgumentParser(description="Экспорт приложения в PDF и проверка числа страниц")
    ap.add_argument("--input", default=os.path.join(HERE, "..", "..", "submission", "appendix.md"))
    ap.add_argument("--output", help="по умолчанию — рядом с входным файлом, расширение .pdf")
    ap.add_argument("--min-pages", type=int, default=10)
    ap.add_argument("--max-pages", type=int, default=15)
    return ap.parse_args()


def main():
    args = parse_args()
    if not os.path.exists(args.input):
        sys.exit(f"Нет файла {args.input}. Укажите --input путь/к/appendix.md")
    try:
        import markdown
    except ImportError:
        sys.exit("Нужен модуль markdown: pip install markdown weasyprint")

    stem = os.path.splitext(args.input)[0]
    html_path, pdf_path = stem + ".html", args.output or stem + ".pdf"
    with open(args.input, encoding="utf-8") as f:
        body = markdown.markdown(f.read(), extensions=["tables", "fenced_code", "sane_lists"])
    body = set_area_widths(set_journal_widths(body))
    html = f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Приложение</title><style>{CSS}</style></head><body>{body}</body></html>"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML: {os.path.relpath(html_path)}")

    try:
        from weasyprint import HTML
        document = HTML(string=html, base_url=os.path.dirname(os.path.abspath(args.input)) + os.sep).render()
        document.write_pdf(pdf_path)
        pages = len(document.pages)
    except Exception as exc:  # WeasyPrint не установлен или не нашёл системные библиотеки
        pages = print_with_chromium(html_path, pdf_path)
        if pages is None:
            print(f"PDF не собран автоматически ({type(exc).__name__}: {exc}).\n"
                  "Откройте HTML в браузере и сохраните как PDF (Ctrl+P), см. docstring скрипта.")
            sys.exit(0)
    print(f"PDF: {os.path.relpath(pdf_path)}, страниц: {pages} (нужно {args.min_pages}–{args.max_pages})")
    if pages > args.max_pages:
        print("Слишком много: уберите demo.png из раздела E или сократите ADR-сводку B.4.")
        sys.exit(1)
    if pages < args.min_pages:
        print("Меньше минимума шаблона: добавьте содержательные разделы (скриншоты, матрица вклада).")


if __name__ == "__main__":
    main()
