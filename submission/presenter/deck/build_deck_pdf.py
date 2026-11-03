#!/usr/bin/env python3
"""
Презентация (8 слайдов 16:9) из исходников deck/project/slides/*.html в PDF.

    pip install weasyprint
    python3 submission/presenter/deck/build_deck_pdf.py            # -> deck/presentation.pdf
    python3 submission/presenter/deck/build_deck_pdf.py out.pdf

Слайды — это HTML-подмножество редактора слайдов (холст 1920x1080, всё позиционировано абсолютно).
Скрипт превращает каждый слайд в страницу PDF: <x-connector> рисуется линией со стрелкой,
<aside> (заметки докладчика) отбрасывается, картинки подставляются из deck/assets.
Шрифты Manrope и Playfair Display подтягиваются с Google Fonts, нужен интернет.
"""
import json
import os
import re
import sys

from weasyprint import HTML

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(HERE, "project")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "presentation.pdf")

# идентификаторы картинок в редакторе слайдов -> файлы рядом со скриптом
IMAGES = {"6c999efb681954d0d9453944eede1061": os.path.join(HERE, "assets", "tests_rose.png")}
FONTS = ("https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;600;700;800"
         "&family=Playfair+Display:wght@400;500;600&display=swap")
HEAD = 20  # длина наконечника стрелки, px


def connector(match):
    attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', match.group(0)))
    x1, y1, x2, y2 = (float(attrs[k]) for k in ("x1", "y1", "x2", "y2"))
    color = re.search(r"color:(#[0-9a-fA-F]{6})", attrs["style"]).group(1)
    width = float(re.search(r"border-width:(\d+)px", attrs["style"]).group(1))
    if y1 == y2:  # горизонтальная
        sign = 1 if x2 > x1 else -1
        end = x2 - sign * HEAD
        side = "left" if sign > 0 else "right"
        line = (f'<div style="position:absolute;left:{min(x1, end)}px;top:{y1 - width / 2}px;'
                f'width:{abs(end - x1)}px;height:{width}px;background:{color}"></div>')
        head = (f'<div style="position:absolute;left:{x2 - HEAD if sign > 0 else x2}px;top:{y1 - 12}px;width:0;height:0;'
                f'border-top:12px solid transparent;border-bottom:12px solid transparent;border-{side}:{HEAD}px solid {color}"></div>')
    else:  # вертикальная
        sign = 1 if y2 > y1 else -1
        end = y2 - sign * HEAD
        side = "top" if sign > 0 else "bottom"
        line = (f'<div style="position:absolute;left:{x1 - width / 2}px;top:{min(y1, end)}px;'
                f'width:{width}px;height:{abs(end - y1)}px;background:{color}"></div>')
        head = (f'<div style="position:absolute;left:{x1 - 12}px;top:{y2 - HEAD if sign > 0 else y2}px;width:0;height:0;'
                f'border-left:12px solid transparent;border-right:12px solid transparent;border-{side}:{HEAD}px solid {color}"></div>')
    return line + head


def page(html):
    style = re.search(r'<section[^>]*style="([^"]*)"', html).group(1)
    inner = re.search(r"<section[^>]*>(.*)</section>", html, re.S).group(1)
    inner = re.sub(r"<aside>.*?</aside>", "", inner, flags=re.S)
    inner = re.sub(r"<x-connector[^>]*></x-connector>", connector, inner)
    inner = re.sub(r'src="/_blob/(\w+)"', lambda m: f'src="file://{IMAGES[m.group(1)]}"', inner)
    return f'<div class="slide" style="{style}">{inner}</div>'


def main():
    with open(os.path.join(PROJECT, "deck.json"), encoding="utf-8") as f:
        order = json.load(f)["order"]
    pages = []
    for slide_id in order:
        with open(os.path.join(PROJECT, "slides", f"{slide_id}.html"), encoding="utf-8") as f:
            pages.append(page(f.read()))
    css = """
@page { size: 1920px 1080px; margin: 0 }
html, body { margin: 0; padding: 0 }
p, h1, h2, h3 { margin: 0 }
.slide { position: relative; width: 1920px; height: 1080px; overflow: hidden; break-after: page }
"""
    html = (f"<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            f"<link rel='stylesheet' href='{FONTS}'><style>{css}</style></head><body>{''.join(pages)}</body></html>")
    document = HTML(string=html, base_url=HERE + os.sep).render()
    document.write_pdf(OUT)
    print(f"{os.path.relpath(OUT)}: {len(document.pages)} страниц")


if __name__ == "__main__":
    main()
