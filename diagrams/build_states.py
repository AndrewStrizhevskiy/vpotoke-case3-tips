#!/usr/bin/env python3
"""Build the payment state diagram (ADR-002) as SVG and draw.io."""
import os

from build_system import FONT, arrow_head, esc, text_w

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1320, 1030

COL = {
    "work": ("#e7f0fb", "#3b6fb6"),
    "ok": ("#e6f4ea", "#2f7d43"),
    "bad": ("#fdeeee", "#c0392b"),
    "cancel": ("#f1f3f5", "#6c757d"),
}
RED = "#c0392b"

# id: (x, y, w, h, kind, final, title, lines)
STATES = {
    "CREATED": (140, 280, 200, 90, "work", False, "CREATED", ["суммы зафиксированы", "QR заблокирован"]),
    "PENDING": (520, 280, 200, 90, "work", False, "PENDING", ["запрос в шлюзе", "QR заблокирован"]),
    "SUCCESS": (960, 110, 220, 90, "ok", True, "SUCCESS", ["деньги списаны", "nonce погашен → QR_USED"]),
    "DECLINED": (960, 420, 220, 90, "bad", True, "DECLINED", ["списания нет", "QR свободен до exp"]),
    "CANCELED": (520, 580, 200, 90, "cancel", True, "CANCELED", ["до отправки в шлюз", "QR свободен до exp"]),
}

# (src, dst, points, label, (lx, ly), anchor, forbidden)
EDGES = [
    (None, "CREATED", [(66, 325), (140, 325)], ["④ POST /tips"], (60, 352), "middle", False),
    ("CREATED", "PENDING", [(340, 325), (520, 325)], ["⑤ confirm", "(client)"], (430, 293), "middle", False),
    ("PENDING", "SUCCESS", [(720, 300), (840, 300), (840, 155), (960, 155)], ["h = 0–7", "(system)"], (900, 123), "middle", False),
    ("PENDING", "DECLINED", [(720, 350), (840, 350), (840, 445), (960, 445)], ["h = 8", "CARD_DECLINED"], (900, 413), "middle", False),
    ("PENDING", "DECLINED", [(680, 370), (680, 490), (960, 490)], ["сверка: 15 мин без ответа,", "GATEWAY_TIMEOUT"], (820, 512), "middle", False),
    ("PENDING", "PENDING", [(580, 280), (580, 220), (660, 220), (660, 280)], ["h = 9: TIMEOUT, ответ 202,", "ждём сверку"], (620, 186), "middle", False),
    ("CREATED", "CANCELED", [(240, 370), (240, 625), (520, 625)], ["cancel (client) или", "exp / 15 мин без confirm (system)"], (380, 589), "middle", False),
    ("PENDING", "CANCELED", [(620, 370), (620, 580)], ["409", "CANCEL_NOT_ALLOWED"], (606, 535), "end", True),
    ("SUCCESS", "CANCELED", [(1180, 155), (1260, 155), (1260, 650), (720, 650)], ["409 CANCEL_NOT_ALLOWED: возвратов в проекте нет"], (990, 626), "middle", True),
]

TABLE_HEAD = ["Переход", "Триггер", "actor", "Событие аудита", "Ответ"]
TABLE = [
    ["— → CREATED", "POST /tips, проверки Q и P пройдены", "client", "payment_created", "201 (повтор ключа: 200)"],
    ["CREATED → PENDING", "POST /payments/{id}/confirm", "client", "payment_submitted", "—"],
    ["PENDING → SUCCESS", "эмулятор: h = 0–7", "system", "payment_confirmed", "200"],
    ["PENDING → DECLINED", "эмулятор: h = 8", "system", "payment_declined", "402 CARD_DECLINED"],
    ["PENDING → PENDING", "эмулятор: h = 9", "system", "— (статус не меняется)", "202 GATEWAY_TIMEOUT"],
    ["PENDING → DECLINED", "сверка: 15 мин без ответа", "system", "payment_declined", "— (фоновый процесс)"],
    ["CREATED → CANCELED", "cancel или истёк exp / 15 мин", "client / system", "payment_canceled", "200"],
    ["PENDING → CANCELED", "cancel", "—", "—", "409 CANCEL_NOT_ALLOWED"],
    ["SUCCESS → CANCELED", "cancel", "—", "—", "409 CANCEL_NOT_ALLOWED"],
]
COLS_X = [60, 260, 560, 690, 900]
TY = 730


def svg():
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         f'font-family="{FONT}" font-style="normal">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         '<text x="40" y="58" font-size="26" font-weight="700" fill="#212529">Жизненный цикл платежа</text>',
         '<text x="40" y="84" font-size="14" fill="#6c757d">кейс 3 · ADR-002 · двойная рамка — финальный статус, '
         'красный пунктир — запрещённый переход</text>',
         '<circle cx="56" cy="325" r="10" fill="#212529"/>']
    for nid, (x, y, w, h, kind, final, title, lines) in STATES.items():
        fill, stroke = COL[kind]
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        if final:
            o.append(f'<rect x="{x + 5}" y="{y + 5}" width="{w - 10}" height="{h - 10}" rx="10" fill="none" stroke="{stroke}" stroke-width="1.2"/>')
        o.append(f'<text x="{x + w / 2}" y="{y + 32}" font-size="18" font-weight="700" text-anchor="middle" fill="{stroke}">{title}</text>')
        for i, ln in enumerate(lines):
            if text_w(ln, 13) > w - 20:
                print(f"overflow in {nid}: {ln}")
            o.append(f'<text x="{x + w / 2}" y="{y + 56 + i * 18}" font-size="13" text-anchor="middle" fill="#343a40">{esc(ln)}</text>')
    for src, dst, pts, label, (lx, ly), anchor, bad in EDGES:
        color = RED if bad else "#495057"
        dash = ' stroke-dasharray="7 5"' if bad else ""
        d = " ".join(f"{px},{py}" for px, py in pts)
        o.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="1.8"{dash}/>')
        o.append(f'<polygon points="{arrow_head(pts[-2], pts[-1])}" fill="{color}"/>')
        if bad:  # cross on the longest segment
            seg = max(zip(pts, pts[1:]), key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))
            cx, cy = (seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2
            o.append(f'<circle cx="{cx}" cy="{cy}" r="11" fill="#ffffff" stroke="{RED}" stroke-width="1.8"/>')
            o.append(f'<path d="M{cx - 5},{cy - 5} L{cx + 5},{cy + 5} M{cx + 5},{cy - 5} L{cx - 5},{cy + 5}" stroke="{RED}" stroke-width="2"/>')
        lw = max(text_w(ln, 12.5) for ln in label) + 8
        rx = {"middle": lx - lw / 2, "start": lx - 4, "end": lx - lw + 4}[anchor]
        o.append(f'<rect x="{rx:.1f}" y="{ly - 12}" width="{lw:.1f}" height="{len(label) * 16 + 2}" fill="#ffffff"/>')
        for i, ln in enumerate(label):
            o.append(f'<text x="{lx}" y="{ly + i * 16}" font-size="12.5" text-anchor="{anchor}" fill="{color}">{esc(ln)}</text>')
    # transition table
    o.append(f'<rect x="40" y="{TY - 40}" width="{W - 80}" height="{(len(TABLE) + 1) * 28 + 30}" rx="10" fill="#fafafa" stroke="#ced4da"/>')
    for j, hd in enumerate(TABLE_HEAD):
        o.append(f'<text x="{COLS_X[j]}" y="{TY - 10}" font-size="13.5" font-weight="700" fill="#212529">{esc(hd)}</text>')
    for i, row in enumerate(TABLE):
        y = TY + 20 + i * 28
        o.append(f'<line x1="56" y1="{y - 19}" x2="{W - 56}" y2="{y - 19}" stroke="#e9ecef"/>')
        bad = row[4].startswith("409")
        for j, cell in enumerate(row):
            nxt = COLS_X[j + 1] if j + 1 < len(COLS_X) else W - 56
            if text_w(cell, 13) > nxt - COLS_X[j] - 10:
                print(f"table overflow: {cell}")
            o.append(f'<text x="{COLS_X[j]}" y="{y}" font-size="13" fill="{RED if bad else "#343a40"}">{esc(cell)}</text>')
    o.append("</svg>")
    return "\n".join(o)


def drawio():
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>',
             '<mxCell id="start" value="" style="ellipse;fillColor=#212529;strokeColor=none;" vertex="1" parent="1">'
             '<mxGeometry x="46" y="315" width="20" height="20" as="geometry"/></mxCell>']
    geo = {"start": (46, 315, 20, 20)}
    for nid, (x, y, w, h, kind, final, title, lines) in STATES.items():
        fill, stroke = COL[kind]
        geo[nid] = (x, y, w, h)
        val = f"<b>{title}</b>" + "".join(f"<br>{esc(l)}" for l in lines)
        style = (f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth=2;fontSize=13;"
                 + ("shape=doubleRect;" if final else ""))
        cells.append(f'<mxCell id="{nid}" value="{esc(val)}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    for i, (src, dst, pts, label, (lx, ly), anchor, bad) in enumerate(EDGES):
        src = src or "start"
        sx, sy, sw, sh = geo[src]
        tx, ty, tw, th = geo[dst]
        (ax, ay), (bx, by) = pts[0], pts[-1]
        color = RED if bad else "#495057"
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;endFill=1;strokeColor={color};"
                 f"fontColor={color};fontSize=12;labelBackgroundColor=#ffffff;"
                 f"exitX={(ax - sx) / sw:.3f};exitY={(ay - sy) / sh:.3f};exitDx=0;exitDy=0;"
                 f"entryX={(bx - tx) / tw:.3f};entryY={(by - ty) / th:.3f};entryDx=0;entryDy=0;"
                 + ("dashed=1;" if bad else ""))
        seg = pts[len(pts) // 2 - 1: len(pts) // 2 + 1]
        mx, my = (seg[0][0] + seg[-1][0]) / 2, (seg[0][1] + seg[-1][1]) / 2
        cy = ly + (len(label) - 1) * 8 - 4
        val = "<br>".join(esc(l) for l in label)
        way = "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in pts[1:-1])
        cells.append(f'<mxCell id="e{i}" value="{esc(val)}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
                     f'<mxGeometry relative="1" as="geometry"><mxPoint x="{lx - mx:.0f}" y="{cy - my:.0f}" as="offset"/>'
                     + (f'<Array as="points">{way}</Array>' if way else "") + '</mxGeometry></mxCell>')
    rows = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in TABLE)
    table = ('<table border="1" cellpadding="4" style="border-collapse:collapse;font-size:12px">'
             "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in TABLE_HEAD) + "</tr>" + rows + "</table>")
    cells.append(f'<mxCell id="table" value="{esc(table)}" style="text;html=1;align=left;verticalAlign=top;" vertex="1" parent="1">'
                 f'<mxGeometry x="40" y="{TY - 40}" width="{W - 80}" height="300" as="geometry"/></mxCell>')
    cells.append('<mxCell id="title" value="&lt;b&gt;Жизненный цикл платежа&lt;/b&gt;" style="text;html=1;fontSize=24;align=left;" '
                 'vertex="1" parent="1"><mxGeometry x="40" y="30" width="600" height="40" as="geometry"/></mxCell>')
    return (f'<mxfile host="app.diagrams.net"><diagram id="states" name="Состояния платежа">'
            f'<mxGraphModel grid="1" gridSize="10" page="1" pageWidth="{W}" pageHeight="{H}">'
            f'<root>{"".join(cells)}</root></mxGraphModel></diagram></mxfile>')


if __name__ == "__main__":
    with open(os.path.join(OUT, "states.svg"), "w", encoding="utf-8") as f:
        f.write(svg())
    with open(os.path.join(OUT, "states.drawio"), "w", encoding="utf-8") as f:
        f.write(drawio())
    print("ok")
