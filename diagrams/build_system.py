#!/usr/bin/env python3
"""Build the system diagram as SVG and draw.io from one model."""
import html
import os

from PIL import ImageFont

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1660, 1000

PAL = {
    "actor": ("#f1f3f5", "#868e96"),
    "api": ("#e7f0fb", "#3b6fb6"),
    "svc": ("#f2f8f2", "#3d8b4f"),
    "mod": ("#ffffff", "#3d8b4f"),
    "ext": ("#fff3e3", "#d9822b"),
    "sec": ("#fff9db", "#b0901a"),
    "db": ("#f3effa", "#7048a8"),
    "aud": ("#fdeeee", "#c0392b"),
    "note": ("#fafafa", "#ced4da"),
}

# id: (x, y, w, h, kind, title, lines)
NODES = {
    "client": (40, 110, 240, 130, "actor", "Клиент", ["телефон, браузер", "сканирует QR,", "выбирает чаевые"]),
    "waiter": (40, 300, 240, 130, "actor", "Официант / мерчант", ["кабинет: QR на чек,", "свои чаевые, отчёты"]),
    "pos": (40, 490, 240, 130, "actor", "Касса (POS)", ["реестр чеков:", "receipt_id, сумма"]),
    "admin": (40, 680, 240, 130, "actor", "Администратор", ["активация и блокировка", "мерчантов, справочники"]),
    "api": (440, 110, 240, 700, "api", "API платформы", [
        "HTTPS (TLS)", "аутентификация (JWT)", "RBAC: 4 роли", "rate limit → 429",
        "формат запроса → 400", "маскирование логов", "",
        "/api/v1/qr", "/api/v1/tips", "/api/v1/payments/{id}/", "    confirm · cancel",
        "/api/v1/reports/tips"]),
    "svc": (780, 110, 460, 470, "svc", "Сервис чаевых — бизнес-логика", []),
    "qr": (800, 160, 420, 110, "mod", "QR-модуль", [
        "генерация: nonce, exp ≤ 24 ч, HMAC-SHA256",
        "проверка: подпись → exp → nonce → мерчант → чек"]),
    "pay": (800, 290, 420, 140, "mod", "Платёжный модуль", [
        "сумма из реестра чеков, расчёт в Decimal",
        "лимиты P-1, Q-6, P-3",
        "идемпотентность P-2, блокировка QR",
        "CREATED → PENDING → SUCCESS / DECLINED"]),
    "rep": (800, 450, 200, 110, "mod", "Отчёты", ["мерчант · официант · день", "чаевые: только SUCCESS"]),
    "rec": (1020, 450, 200, 110, "mod", "Сверка (job)", ["PENDING > 15 мин →", "DECLINED,", "GATEWAY_TIMEOUT"]),
    "sec": (1360, 160, 260, 110, "sec", "Секреты", ["env: TIPS_HMAC_SECRET,", "пароль БД;", "не в Git и не в логах"]),
    "emu": (1360, 290, 260, 140, "ext", "Эмулятор шлюза", [
        "1) gateway_scenarios.csv", "2) h = md5(qr_id + nonce) mod 10", "0–7 SUCCESS · 8 DECLINED", "9 TIMEOUT"]),
    "db": (780, 660, 460, 170, "db", "Хранилище", [
        "merchants, employees (реквизиты ****NNNN)",
        "receipts, qr_links",
        "payments (+ payments_out.csv), idempotency_keys",
        "reports — пересчитываемый агрегат"]),
    "aud": (1360, 660, 260, 170, "aud", "Аудит", [
        "audit_events, только дописывание", "событие на каждый переход:",
        "event_id, payment_id, qr_id,", "action, from → to, amount_total,", "actor, at"]),
}

# (src, dst, points, label lines, label anchor (x, y), dashed)
EDGES = [
    ("client", "api", [(280, 160), (440, 160)], ["③ GET /qr/{id}?sig=", "④ POST /tips", "⑤ confirm · cancel"], (360, 112), False),
    ("api", "client", [(440, 205), (280, 205)], ["QRResponse · Payment", "Error {code, retryable}"], (360, 224), False),
    ("waiter", "api", [(280, 350), (440, 350)], ["① POST /qr", "{receipt_id}"], (360, 318), False),
    ("api", "waiter", [(440, 390), (280, 390)], ["② ссылка ?qr=&sig=", "+ картинка QR"], (360, 409), False),
    ("pos", "api", [(280, 555), (440, 555)], ["чек {receipt_id,", "amount}"], (360, 523), False),
    ("admin", "api", [(280, 745), (440, 745)], ["статус мерчанта,", "справочники"], (360, 713), False),
    ("api", "svc", [(680, 345), (780, 345)], ["запрос", "+ роль"], (730, 313), False),
    ("svc", "api", [(780, 385), (680, 385)], ["ответ", "или ошибка"], (730, 404), False),
    ("sec", "qr", [(1360, 215), (1220, 215)], ["HMAC-ключ"], (1290, 199), False),
    ("pay", "emu", [(1220, 335), (1360, 335)], ["⑥ payment_id,", "qr_id, nonce"], (1290, 303), False),
    ("emu", "pay", [(1360, 395), (1220, 395)], ["⑦ SUCCESS /", "DECLINED /", "TIMEOUT"], (1300, 414), False),
    ("rec", "emu", [(1220, 500), (1490, 500), (1490, 430)], ["запрос статуса"], (1355, 491), True),
    ("svc", "db", [(1010, 580), (1010, 660)], ["чтение / запись"], (1020, 625), False),
    ("svc", "aud", [(1240, 545), (1490, 545), (1490, 660)], ["⑧ событие перехода"], (1365, 566), False),
]

FLOW = [
    "Основной поток: ① официант запрашивает QR на чек → ② получает ссылку с подписью → ③ клиент открывает её и видит сумму из реестра →",
    "④ создаёт платёж (CREATED) → ⑤ подтверждает (PENDING) → ⑥ запрос в шлюз → ⑦ исход SUCCESS / DECLINED / TIMEOUT → ⑧ событие в аудит.",
    "Прототип: CLI заменяет клиента и API, CSV-файлы — хранилище, emulate_gateway() — шлюз. Оранжевым отмечена эмулируемая внешняя система.",
]

FONT = "Inter, 'DejaVu Sans', sans-serif"
FONT_FILE = "/usr/share/fonts/opentype/inter/Inter-Regular.otf"


def text_w(s, size):
    return ImageFont.truetype(FONT_FILE, size).getlength(s)


def arrow_head(p, q, size=11):
    """Triangle at q pointing from p to q (segments are axis-aligned)."""
    dx = (q[0] > p[0]) - (q[0] < p[0])
    dy = (q[1] > p[1]) - (q[1] < p[1])
    bx, by = q[0] - dx * size, q[1] - dy * size
    nx, ny = -dy * size * 0.45, dx * size * 0.45
    return f"{q[0]},{q[1]} {bx + nx},{by + ny} {bx - nx},{by - ny}"


def esc(s):
    return html.escape(s, quote=True)


def svg():
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}" font-style="normal">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           '<text x="40" y="58" font-size="26" font-weight="700" fill="#212529">Онлайн-чаевые — схема системы</text>',
           '<text x="40" y="84" font-size="14" fill="#6c757d">кейс 3 · блоки, потоки данных и их содержимое</text>']
    for nid, (x, y, w, h, kind, title, lines) in NODES.items():
        fill, stroke = PAL[kind]
        sw = 2.5 if kind in ("api", "svc") else 1.5
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
        out.append(f'<text x="{x + 14}" y="{y + 28}" font-size="17" font-weight="700" fill="{stroke}">{esc(title)}</text>')
        for ln in [title] + lines:
            if text_w(ln, 17 if ln == title else 13.5) > w - 24:
                print(f"overflow in {nid}: {ln}")
        if 54 + (len(lines) - 1) * 19 > h - 8:
            print(f"vertical overflow in {nid}")
        for i, ln in enumerate(lines):
            out.append(f'<text x="{x + 14}" y="{y + 54 + i * 19}" font-size="13.5" fill="#343a40" xml:space="preserve">{esc(ln)}</text>')
    for src, dst, pts, label, (lx, ly), dashed in EDGES:
        d = " ".join(f"{px},{py}" for px, py in pts)
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        out.append(f'<polyline points="{d}" fill="none" stroke="#495057" stroke-width="1.8"{dash}/>')
        out.append(f'<polygon points="{arrow_head(pts[-2], pts[-1])}" fill="#495057"/>')
        lw = max(text_w(ln, 12.5) for ln in label) + 8
        out.append(f'<rect x="{lx - lw / 2:.1f}" y="{ly - 12}" width="{lw:.1f}" height="{len(label) * 16 + 2}" fill="#ffffff"/>')
        for i, ln in enumerate(label):
            out.append(f'<text x="{lx}" y="{ly + i * 16}" font-size="12.5" text-anchor="middle" fill="#212529">{esc(ln)}</text>')
    fy = 870
    fill, stroke = PAL["note"]
    out.append(f'<rect x="40" y="{fy}" width="{W - 80}" height="100" rx="10" fill="{fill}" stroke="{stroke}"/>')
    for i, ln in enumerate(FLOW):
        out.append(f'<text x="58" y="{fy + 30 + i * 26}" font-size="14" fill="#343a40">{esc(ln)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def drawio():
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for nid, (x, y, w, h, kind, title, lines) in NODES.items():
        fill, stroke = PAL[kind]
        val = f"<b>{esc(title)}</b>" + "".join(f"<br>{esc(l) or '&nbsp;'}" for l in lines)
        style = (f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                 f"verticalAlign=top;align=left;spacingLeft=10;spacingTop=6;fontSize=13;fontColor=#343a40;"
                 f"strokeWidth={2 if kind in ('api', 'svc') else 1};")
        cells.append(f'<mxCell id="{nid}" value="{esc(val)}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    for i, (src, dst, pts, label, (lx, ly), dashed) in enumerate(EDGES):
        sx, sy, sw_, sh = NODES[src][:4]
        tx, ty, tw, th = NODES[dst][:4]
        (p0x, p0y), (pnx, pny) = pts[0], pts[-1]
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;endFill=1;strokeColor=#495057;"
                 f"fontSize=12;labelBackgroundColor=#ffffff;"
                 f"exitX={(p0x - sx) / sw_:.3f};exitY={(p0y - sy) / sh:.3f};exitDx=0;exitDy=0;"
                 f"entryX={(pnx - tx) / tw:.3f};entryY={(pny - ty) / th:.3f};entryDx=0;entryDy=0;"
                 + ("dashed=1;" if dashed else ""))
        # label offset from the edge midpoint (draw.io places labels there)
        seg = pts[len(pts) // 2 - 1: len(pts) // 2 + 1] if len(pts) > 2 else pts
        mx = (seg[0][0] + seg[-1][0]) / 2
        my = (seg[0][1] + seg[-1][1]) / 2
        cy = ly + (len(label) - 1) * 8 - 4
        val = "<br>".join(esc(l) for l in label)
        way = "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in pts[1:-1])
        cells.append(f'<mxCell id="e{i}" value="{esc(val)}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
                     f'<mxGeometry relative="1" as="geometry"><mxPoint x="{lx - mx:.0f}" y="{cy - my:.0f}" as="offset"/>'
                     + (f'<Array as="points">{way}</Array>' if way else "") + '</mxGeometry></mxCell>')
    cells.append('<mxCell id="title" value="&lt;b&gt;Онлайн-чаевые — схема системы&lt;/b&gt;" style="text;html=1;fontSize=24;align=left;" '
                 'vertex="1" parent="1"><mxGeometry x="40" y="30" width="700" height="40" as="geometry"/></mxCell>')
    flow = "<br>".join(esc(l) for l in FLOW)
    cells.append(f'<mxCell id="flow" value="{esc(flow)}" style="rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor=#fafafa;'
                 f'strokeColor=#ced4da;align=left;spacingLeft=14;fontSize=14;" vertex="1" parent="1">'
                 f'<mxGeometry x="40" y="870" width="{W - 80}" height="100" as="geometry"/></mxCell>')
    body = "".join(cells)
    return (f'<mxfile host="app.diagrams.net"><diagram id="system" name="Схема системы">'
            f'<mxGraphModel dx="{W}" dy="{H}" grid="1" gridSize="10" page="1" pageWidth="{W}" pageHeight="{H}">'
            f'<root>{body}</root></mxGraphModel></diagram></mxfile>')


if __name__ == "__main__":
    with open(os.path.join(OUT, "system.svg"), "w", encoding="utf-8") as f:
        f.write(svg())
    with open(os.path.join(OUT, "system.drawio"), "w", encoding="utf-8") as f:
        f.write(drawio())
    print("ok")
