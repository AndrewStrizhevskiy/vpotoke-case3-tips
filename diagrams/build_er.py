#!/usr/bin/env python3
"""Build the ER diagram (ADR-005) as SVG and draw.io."""
import os

from build_system import FONT, arrow_head, esc, text_w

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1560, 900
BW, HEAD, ROW = 300, 38, 22

KEY_COL = {"PK": "#b8860b", "FK": "#3b6fb6", "UK": "#7048a8", "PK FK": "#b8860b"}

# id: (x, y, colour, external, title, rows[(key, name, type/constraint)])
ENT = {
    "merchant": (60, 120, "#3d8b4f", False, "Merchant", [
        ("PK", "merchant_id", "m_NNN"), ("", "name", "1–100"),
        ("", "status", "ACTIVE / BLOCKED / PENDING"), ("", "description", "0–200, необяз.")]),
    "receipt": (440, 120, "#868e96", True, "Receipt (реестр POS)", [
        ("PK", "receipt_id", "r_NNNN"), ("PK FK", "merchant_id", "→ Merchant"),
        ("", "amount", "decimal, 0 < x ≤ 1 000 000"), ("", "currency", "RUB")]),
    "report": (1200, 120, "#7048a8", False, "Report", [
        ("PK", "report_id", "rep_NNNN"), ("FK", "merchant_id", "→ Merchant"),
        ("", "period", "YYYY-MM"), ("", "tips_total", "≥ 0, по SUCCESS"),
        ("", "payments_count", "≥ 0"), ("", "avg_tip_pct", "0–50"), ("", "success_rate", "0–1")]),
    "employee": (60, 420, "#3d8b4f", False, "Employee", [
        ("PK", "employee_id", "e_NNN"), ("FK", "merchant_id", "→ Merchant"),
        ("", "name", "1–100"), ("", "payment_details", "только ****NNNN")]),
    "qr": (440, 420, "#3b6fb6", False, "QR", [
        ("PK", "qr_id", "qr_NNN"), ("FK", "merchant_id", "→ Merchant"),
        ("FK", "employee_id", "→ Employee"), ("FK", "receipt_id", "→ Receipt"),
        ("UK", "nonce", "6 hex, одноразовый"), ("", "exp", "ISO 8601, ≤ +24 ч"),
        ("", "signature", "HMAC-SHA256, 16 hex")]),
    "payment": (820, 420, "#d9822b", False, "Payment", [
        ("PK", "payment_id", "p_NNNN"), ("FK", "qr_id", "→ QR"),
        ("FK", "merchant_id", "денорм., отчёты"), ("FK", "employee_id", "денорм., отчёты"),
        ("", "amount", "0 < x ≤ 1 000 000"), ("", "tip", "0 ≤ x ≤ amount × 0.5"),
        ("", "amount_total", "amount + tip"), ("", "status", "5 статусов"),
        ("UK", "idempotency_key", "UUID, уник. в qr_id"), ("", "error_code", "необяз."),
        ("", "timestamp", "ISO 8601, UTC")]),
    "audit": (1200, 420, "#c0392b", False, "AuditEvent", [
        ("PK", "event_id", "evt_NNNN"), ("FK", "payment_id", "→ Payment"),
        ("FK", "qr_id", "денорм."), ("", "action", "payment_created…"),
        ("", "from_status", "статус или null"), ("", "to_status", "статус"),
        ("", "amount_total", "decimal"), ("", "actor", "client / system / officer"),
        ("", "at", "ISO 8601, UTC")]),
}

# (src, dst, points, (one_xy), (many_xy), label, label_xy)
REL = [
    ("merchant", "employee", [(210, 256), (210, 420)], (222, 276), (222, 410), "работают", (250, 340)),
    ("merchant", "receipt", [(360, 170), (440, 170)], (368, 162), (426, 162), "", None),
    ("merchant", "qr", [(360, 230), (400, 230), (400, 480), (440, 480)], (368, 222), (426, 472), "", None),
    ("receipt", "qr", [(590, 256), (590, 420)], (602, 276), (602, 410), "перевыпуск после exp", (660, 340)),
    ("employee", "qr", [(360, 560), (440, 560)], (368, 552), (426, 552), "", None),
    ("qr", "payment", [(740, 520), (820, 520)], (748, 512), (806, 512), "попытки", (780, 540)),
    ("payment", "audit", [(1120, 520), (1200, 520)], (1128, 512), (1186, 512), "переходы", (1160, 540)),
    ("merchant", "report", [(110, 120), (110, 100), (1350, 100), (1350, 120)], (98, 114), (1362, 114), "отчёт за период", (760, 92)),
]

NOTE = [
    "Связи 1:N. Подписи у концов линий: 1 — сторона «один», N — сторона «много».",
    "Payment.merchant_id / employee_id и AuditEvent.qr_id — денормализация ради отчётов, отдельными линиями не показаны.",
    "Инвариант: QR.merchant_id = Employee.merchant_id = Receipt.merchant_id. Receipt — внешний реестр кассы (пунктир, ADR-003).",
]


def box_h(rows):
    return HEAD + len(rows) * ROW + 10


def svg():
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         f'font-family="{FONT}" font-style="normal">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         '<text x="40" y="50" font-size="26" font-weight="700" fill="#212529">ER-модель</text>',
         '<text x="200" y="50" font-size="14" fill="#6c757d">кейс 3 · ADR-005 · PK — первичный ключ, FK — внешний, UK — уникальный</text>']
    for src, dst, pts, one, many, label, lxy in REL:
        d = " ".join(f"{x},{y}" for x, y in pts)
        o.append(f'<polyline points="{d}" fill="none" stroke="#495057" stroke-width="1.6"/>')
        o.append(f'<polygon points="{arrow_head(pts[-2], pts[-1], 9)}" fill="#495057"/>')
        for txt, (x, y) in (("1", one), ("N", many)):
            o.append(f'<text x="{x}" y="{y}" font-size="14" font-weight="700" text-anchor="middle" fill="#212529">{txt}</text>')
        if label:
            lw = text_w(label, 12) + 8
            o.append(f'<rect x="{lxy[0] - lw / 2:.1f}" y="{lxy[1] - 12}" width="{lw:.1f}" height="17" fill="#ffffff"/>')
            o.append(f'<text x="{lxy[0]}" y="{lxy[1]}" font-size="12" text-anchor="middle" fill="#6c757d">{esc(label)}</text>')
    for eid, (x, y, col, ext, title, rows) in ENT.items():
        h = box_h(rows)
        dash = ' stroke-dasharray="7 5"' if ext else ""
        o.append(f'<rect x="{x}" y="{y}" width="{BW}" height="{h}" rx="8" fill="#ffffff" stroke="{col}" stroke-width="2"{dash}/>')
        o.append(f'<path d="M{x},{y + HEAD} H{x + BW}" stroke="{col}" stroke-width="1.2"{dash}/>')
        o.append(f'<text x="{x + 12}" y="{y + 25}" font-size="16" font-weight="700" fill="{col}">{esc(title)}</text>')
        for i, (key, name, cons) in enumerate(rows):
            ry = y + HEAD + 18 + i * ROW
            if key:
                o.append(f'<text x="{x + 10}" y="{ry}" font-size="10.5" font-weight="700" fill="{KEY_COL[key]}">{key}</text>')
            weight = "700" if "PK" in key else "400"
            o.append(f'<text x="{x + 52}" y="{ry}" font-size="13" font-weight="{weight}" fill="#212529">{esc(name)}</text>')
            o.append(f'<text x="{x + BW - 10}" y="{ry}" font-size="11.5" text-anchor="end" fill="#6c757d">{esc(cons)}</text>')
            if text_w(name, 13) + text_w(cons, 11.5) > BW - 52 - 18:
                print(f"overflow in {eid}: {name} / {cons}")
    ny = 760
    o.append(f'<rect x="40" y="{ny}" width="{W - 80}" height="100" rx="10" fill="#fafafa" stroke="#ced4da"/>')
    for i, ln in enumerate(NOTE):
        o.append(f'<text x="58" y="{ny + 30 + i * 26}" font-size="14" fill="#343a40">{esc(ln)}</text>')
    o.append("</svg>")
    return "\n".join(o)


def drawio():
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for eid, (x, y, col, ext, title, rows) in ENT.items():
        body = "".join(f'<tr><td><b>{k}</b></td><td>{esc(n)}</td><td style="color:#6c757d">{esc(c)}</td></tr>'
                       for k, n, c in rows)
        val = (f'<div style="font-weight:bold;color:{col};padding:4px">{esc(title)}</div>'
               f'<table style="font-size:11px;width:100%">{body}</table>')
        style = (f"rounded=1;arcSize=3;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor={col};strokeWidth=2;"
                 f"verticalAlign=top;align=left;overflow=fill;" + ("dashed=1;" if ext else ""))
        cells.append(f'<mxCell id="{eid}" value="{esc(val)}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{x}" y="{y}" width="{BW}" height="{box_h(rows)}" as="geometry"/></mxCell>')
    for i, (src, dst, pts, one, many, label, lxy) in enumerate(REL):
        sx, sy = ENT[src][:2]
        tx, ty = ENT[dst][:2]
        sh, th = box_h(ENT[src][5]), box_h(ENT[dst][5])
        (ax, ay), (bx, by) = pts[0], pts[-1]
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;endFill=1;strokeColor=#495057;"
                 f"fontSize=12;labelBackgroundColor=#ffffff;"
                 f"exitX={(ax - sx) / BW:.3f};exitY={(ay - sy) / sh:.3f};exitDx=0;exitDy=0;"
                 f"entryX={(bx - tx) / BW:.3f};entryY={(by - ty) / th:.3f};entryDx=0;entryDy=0;")
        val = f"1 : N{(' — ' + esc(label)) if label else ''}"
        way = "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in pts[1:-1])
        cells.append(f'<mxCell id="r{i}" value="{esc(val)}" style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
                     '<mxGeometry relative="1" as="geometry">'
                     + (f'<Array as="points">{way}</Array>' if way else "") + '</mxGeometry></mxCell>')
    note = "<br>".join(esc(n) for n in NOTE)
    cells.append(f'<mxCell id="note" value="{esc(note)}" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#fafafa;'
                 f'strokeColor=#ced4da;align=left;spacingLeft=14;fontSize=13;" vertex="1" parent="1">'
                 f'<mxGeometry x="40" y="760" width="{W - 80}" height="100" as="geometry"/></mxCell>')
    return (f'<mxfile host="app.diagrams.net"><diagram id="er" name="ER-модель">'
            f'<mxGraphModel grid="1" gridSize="10" page="1" pageWidth="{W}" pageHeight="{H}">'
            f'<root>{"".join(cells)}</root></mxGraphModel></diagram></mxfile>')


if __name__ == "__main__":
    with open(os.path.join(OUT, "er.svg"), "w", encoding="utf-8") as f:
        f.write(svg())
    with open(os.path.join(OUT, "er.drawio"), "w", encoding="utf-8") as f:
        f.write(drawio())
    print("ok")
