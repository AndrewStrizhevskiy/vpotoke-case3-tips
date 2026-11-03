#!/usr/bin/env python3
"""Макет диалоговой формы чаевых (4 экрана) в SVG из одного описания. Данные — qr_001 из seed."""
import os

from build_system import FONT, esc, text_w

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1760, 1050
PW, PH = 340, 640           # телефон
PY = 150                    # верх телефонов
XS = [60, 480, 900, 1320]   # левые края
INK, MUTED, LINE = "#1B2430", "#6A7179", "#D5D8DD"
ACC, ACC_BG = "#2F5DA8", "#E3ECF9"
OK, OK_BG = "#2F7D43", "#E6F4EA"
BAD, BAD_BG = "#C0392B", "#FDEEEE"

TITLES = ["1. Приветствие", "2. Выбор чаевых", "3. Подтверждение", "4. Результат"]
NOTES = [
    "из QR: заведение, официант, чек и сумма из реестра кассы",
    "проценты 0–20 % или своя сумма; итог пересчитывается сразу",
    "последний взгляд на суммы; кнопка создаёт и подтверждает платёж",
    "три исхода: успех, отказ, таймаут — формулировки из ADR-006",
]


def wrap(text, size, max_width):
    """Перенос по словам: подпись не рвёт слова посередине."""
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and text_w(trial, size) > max_width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur]


def phone(x, body):
    o = [f'<rect x="{x}" y="{PY}" width="{PW}" height="{PH}" rx="34" fill="#ffffff" stroke="{INK}" stroke-width="3"/>',
         f'<rect x="{x + PW / 2 - 44}" y="{PY + 14}" width="88" height="8" rx="4" fill="{LINE}"/>']
    return o + body


def t(x, y, s, size=18, color=INK, weight="400", anchor="start"):
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}">{esc(s)}</text>')


def button(x, y, w, label, fill=ACC, color="#ffffff", h=54):
    return [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}"/>',
            t(x + w / 2, y + h / 2 + 7, label, 20, color, "600", "middle")]


def row(x, y, w, left, right, bold=False, color=INK):
    wt = "700" if bold else "400"
    return [t(x, y, left, 19, color, wt), t(x + w, y, right, 19, color, wt, "end")]


def screen1(x):
    c, w = x + 28, PW - 56
    o = [t(c, PY + 70, "Вкус-Сервис Тверская", 22, ACC, "700"), t(c, PY + 98, "чек r_1001", 17, MUTED)]
    o += [f'<rect x="{c}" y="{PY + 140}" width="{w}" height="150" rx="16" fill="{ACC_BG}"/>',
          t(c + 20, PY + 182, "Вас обслуживал(а)", 17, MUTED), t(c + 20, PY + 218, "Иванов И.", 28, INK, "700"),
          t(c + 20, PY + 262, "Сумма чека: 637.00 ₽", 20, INK)]
    o += [t(c, PY + 340, "Оставьте чаевые официанту:", 19, INK),
          t(c, PY + 368, "они пойдут лично ему на карту", 17, MUTED)]
    return o + button(c, PY + PH - 100, w, "Продолжить")


def chip(x, y, w, label, active):
    fill, color, stroke = (ACC, "#ffffff", ACC) if active else ("#ffffff", INK, LINE)
    return [f'<rect x="{x}" y="{y}" width="{w}" height="48" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="2"/>',
            t(x + w / 2, y + 31, label, 20, color, "600", "middle")]


def screen2(x):
    c, w = x + 28, PW - 56
    o = [t(c, PY + 64, "Размер чаевых", 24, INK, "700"), t(c, PY + 94, "Чек: 637.00 ₽", 18, MUTED)]
    cw = (w - 20) // 3
    labels = ["0 %", "5 %", "10 %", "15 %", "20 %", "Своя"]
    for i, lab in enumerate(labels):
        o += chip(c + (i % 3) * (cw + 10), PY + 130 + (i // 3) * 60, cw, lab, i == 1)
    o += [f'<rect x="{c}" y="{PY + 280}" width="{w}" height="170" rx="16" fill="#F6F5F1"/>']
    o += row(c + 20, PY + 322, w - 40, "Чек", "637.00 ₽", color=MUTED)
    o += row(c + 20, PY + 356, w - 40, "Чаевые 5 %", "31.85 ₽", color=MUTED)
    o += [f'<line x1="{c + 20}" y1="{PY + 376}" x2="{c + w - 20}" y2="{PY + 376}" stroke="{LINE}" stroke-width="2"/>']
    o += row(c + 20, PY + 416, w - 40, "Итого", "668.85 ₽", bold=True)
    o += [t(c, PY + 482, "«Своя» — любая сумма до 50 % чека", 16, MUTED)]
    return o + button(c, PY + PH - 100, w, "Оплатить")


def screen3(x):
    c, w = x + 28, PW - 56
    o = [t(c, PY + 64, "Подтвердите оплату", 24, INK, "700")]
    o += [f'<rect x="{c}" y="{PY + 100}" width="{w}" height="230" rx="16" fill="#F6F5F1"/>']
    o += row(c + 20, PY + 148, w - 40, "Заведение", "Тверская", color=MUTED)
    o += row(c + 20, PY + 190, w - 40, "Официант", "Иванов И.", color=MUTED)
    o += row(c + 20, PY + 232, w - 40, "Чек", "637.00 ₽")
    o += row(c + 20, PY + 270, w - 40, "Чаевые", "31.85 ₽")
    o += [f'<line x1="{c + 20}" y1="{PY + 288}" x2="{c + w - 20}" y2="{PY + 288}" stroke="{LINE}" stroke-width="2"/>']
    o += row(c + 20, PY + 316, w - 40, "К оплате", "668.85 ₽", bold=True)
    o += [t(c, PY + 372, "Нажимая кнопку, вы подтверждаете", 16, MUTED), t(c, PY + 394, "списание всей суммы с карты", 16, MUTED)]
    o += button(c, PY + PH - 160, w, "Подтвердить оплату")
    o += [t(x + PW / 2, PY + PH - 70, "Назад", 20, ACC, "600", "middle")]
    return o


def screen4(x):
    c, w = x + 28, PW - 56
    cx = x + PW / 2
    o = [f'<circle cx="{cx}" cy="{PY + 170}" r="58" fill="{OK_BG}"/>',
         f'<path d="M{cx - 26},{PY + 172} L{cx - 7},{PY + 192} L{cx + 28},{PY + 150}" fill="none" stroke="{OK}" '
         f'stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>',
         t(cx, PY + 288, "Спасибо!", 30, OK, "700", "middle"),
         t(cx, PY + 328, "Чаевые отправлены", 22, INK, "600", "middle"),
         t(cx, PY + 372, "668.85 ₽ · чек r_1001", 18, MUTED, "400", "middle")]
    return o + button(c, PY + PH - 100, w, "Готово", fill="#ffffff", color=ACC) \
        + [f'<rect x="{c}" y="{PY + PH - 100}" width="{w}" height="54" rx="14" fill="none" stroke="{ACC}" stroke-width="2"/>']


def outcomes():
    """Два остальных исхода экрана 4."""
    y = PY + PH + 130
    o = [t(60, y - 22, "Экран 4: другие исходы", 20, INK, "700")]
    cards = [("Отказ шлюза", "402 CARD_DECLINED", "Оплата не прошла, попробуйте другой способ", BAD, BAD_BG),
             ("Таймаут шлюза", "202 GATEWAY_TIMEOUT", "Обрабатываем, проверьте через минуту", "#B5651D", "#FFF3E3"),
             ("QR устарел", "410 QR_EXPIRED", "QR устарел, попросите официанта новый", MUTED, "#F1F3F5")]
    for i, (name, code, msg, col, bg) in enumerate(cards):
        x = 60 + i * 560
        o += [f'<rect x="{x}" y="{y}" width="535" height="92" rx="14" fill="{bg}" stroke="{col}" stroke-width="1.5"/>',
              t(x + 22, y + 34, name, 20, col, "700"), t(x + 513, y + 34, code, 17, col, "400", "end"),
              t(x + 22, y + 68, msg, 19, INK)]
        if text_w(msg, 19) > 490:
            print("overflow:", msg)
    return o


def svg():
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         f'font-family="{FONT}" font-style="normal">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         t(40, 56, "Диалоговая форма чаевых — 4 экрана", 28, INK, "700"),
         t(40, 86, "клиент открывает ссылку из QR: пример qr_001, чек 637.00 ₽, чаевые 5 %", 16, MUTED)]
    builders = [screen1, screen2, screen3, screen4]
    for i, (x, build) in enumerate(zip(XS, builders)):
        o += [t(x + PW / 2, PY - 18, TITLES[i], 21, ACC, "700", "middle")]
        o += phone(x, build(x))
        for j, line in enumerate(wrap(NOTES[i], 15, PW + 50)):
            o += [t(x + PW / 2, PY + PH + 32 + j * 20, line, 15, MUTED, "400", "middle")]
    for i in range(3):
        xa = XS[i] + PW + 14
        o += [f'<polygon points="{xa},{PY + PH / 2 - 10} {xa + 52},{PY + PH / 2} {xa},{PY + PH / 2 + 10}" fill="{LINE}"/>']
    o += outcomes()
    return "\n".join(o + ["</svg>"])


if __name__ == "__main__":
    with open(os.path.join(OUT, "dialog.svg"), "w", encoding="utf-8") as f:
        f.write(svg())
    print("ok")
