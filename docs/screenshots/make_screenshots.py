#!/usr/bin/env python3
"""
Скриншоты для слайда 6 и раздела E приложения: вывод реальных команд, отрисованный как окно терминала.

    export TIPS_HMAC_SECRET=<демо-ключ из README>
    python3 docs/screenshots/make_screenshots.py            # все четыре
    python3 docs/screenshots/make_screenshots.py commit     # только указанные: tests, md5, demo, commit

Команды запускаются здесь и сейчас, картинки — их настоящий вывод, а не ручная вёрстка.
Скриншот коммита зависит от момента запуска: после создания ветки team_N_case_3 пересоздайте его.
"""
import os
import re
import subprocess
import sys

from PIL import Image, ImageColor, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SIZE, PAD, BAR, COLS = 22, 30, 48, 112

BG, BAR_BG, FG = "#14181F", "#222833", "#E6E8EB"
COLORS = {"90": "#8A93A0", "31": "#FF7B7B", "32": "#5FD38D", "33": "#F2C94C", "34": "#6CB2FF", "36": "#5CE1E6"}

SGR = re.compile(r"\x1b\[([0-9;]*)m")

# имя -> (заголовок окна, [(команда для показа, команда запуска, рабочий каталог)])
SCENES = {
    "tests": ("python3 prototype/run_tests.py", [
        ("export TIPS_HMAC_SECRET=<демо-ключ организаторов>", None, ROOT),
        ("python3 prototype/run_tests.py", [sys.executable, "-I", "prototype/run_tests.py"], ROOT)]),
    "md5": ("md5sum — данные не менялись", [
        ("cd prototype/01_данные && md5sum -c MD5SUMS", ["md5sum", "-c", "MD5SUMS"],
         os.path.join(ROOT, "prototype", "01_данные"))]),
    "demo": ("python3 prototype/demo.py", [
        ("python3 prototype/demo.py", [sys.executable, "-I", "prototype/demo.py", "--color"], ROOT)]),
    "commit": ("git log — последний коммит", [
        ("git log -1 --date=short --format='%h  %s%n%an, %ad'", ["git", "log", "-1", "--date=short",
                                                            "--format=%h  %s%n%an, %ad"], ROOT),
        ("git branch --show-current", ["git", "branch", "--show-current"], ROOT)]),
}


def colorize(line):
    """Подсветка итогов в обычном (не цветном) выводе."""
    line = re.sub(r"^(PASS)", "\x1b[32m\\1\x1b[0m", line)
    line = re.sub(r"^(FAIL)", "\x1b[31m\\1\x1b[0m", line)
    line = re.sub(r"(: )(OK)$", "\\1\x1b[32m\\2\x1b[0m", line)
    return re.sub(r"^(Итого:.*)$", "\x1b[1m\\1\x1b[0m", line)


def parse(line):
    """Строка с ANSI -> список (символ, цвет, жирный)."""
    cells, color, bold, pos = [], FG, False, 0
    for m in SGR.finditer(line):
        cells += [(ch, color, bold) for ch in line[pos:m.start()]]
        for code in (m.group(1) or "0").split(";"):
            if code in ("", "0"):
                color, bold = FG, False
            elif code == "1":
                bold = True
            elif code in COLORS:
                color = COLORS[code]
        pos = m.end()
    return cells + [(ch, color, bold) for ch in line[pos:]]


def wrap(cells):
    """Длинные строки переносятся по пробелам и с отступом, чтобы картинка не была шире экрана."""
    out, first = [], True
    while cells:
        width = COLS if first else COLS - 4
        if len(cells) <= width:
            cut = len(cells)
        else:
            spaces = [i for i, (ch, _, _) in enumerate(cells[:width + 1]) if ch == " "]
            cut = spaces[-1] if spaces else width
        out.append(([] if first else [(" ", FG, False)] * 4) + cells[:cut])
        cells, first = cells[cut:], False
        while cells and cells[0][0] == " ":
            cells = cells[1:]
    return out or [[]]


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return (proc.stdout + proc.stderr).rstrip("\n").split("\n")


def render(name):
    title, commands = SCENES[name]
    lines = []
    for shown, cmd, cwd in commands:
        lines.append(parse(f"\x1b[34m$\x1b[0m {shown}"))
        if cmd is not None:
            lines += [parse(colorize(line)) for line in run(cmd, cwd)]
    lines = [row for line in lines for row in wrap(line)]
    font, bold = ImageFont.truetype(FONT, SIZE), ImageFont.truetype(FONT_BOLD, SIZE)
    adv, step = font.getlength("M"), int(SIZE * 1.5)
    cols = max(len(line) for line in lines)
    width, height = int(PAD * 2 + cols * adv), BAR + PAD * 2 + step * len(lines)
    img = Image.new("RGB", (width, height), ImageColor.getrgb(BG))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, BAR], fill=BAR_BG)
    for i, dot in enumerate(("#FF5F56", "#FFBD2E", "#27C93F")):
        d.ellipse([PAD + i * 30, BAR / 2 - 8, PAD + i * 30 + 16, BAR / 2 + 8], fill=dot)
    d.text((width / 2, BAR / 2), title, font=font, fill=COLORS["90"], anchor="mm")
    for row, cells in enumerate(lines):
        y = BAR + PAD + row * step
        for col, (ch, color, is_bold) in enumerate(cells):
            if ch != " ":
                d.text((PAD + col * adv, y), ch, font=bold if is_bold else font, fill=color)
    path = os.path.join(HERE, f"{name}.png")
    img.save(path)
    print(f"{os.path.relpath(path, ROOT)}: {width}x{height}, строк {len(lines)}")


def main():
    names = sys.argv[1:] or list(SCENES)
    unknown = [n for n in names if n not in SCENES]
    if unknown:
        sys.exit(f"Неизвестно: {unknown}. Доступно: {list(SCENES)}")
    if {"tests", "demo"} & set(names) and not os.environ.get("TIPS_HMAC_SECRET"):
        sys.exit("Задайте TIPS_HMAC_SECRET (демо-ключ организаторов, см. README)")
    for name in names:
        render(name)


if __name__ == "__main__":
    main()
