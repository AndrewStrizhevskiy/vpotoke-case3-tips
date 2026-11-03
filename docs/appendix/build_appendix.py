#!/usr/bin/env python3
"""Собрать приложение A–F (docs/appendix/appendix.md) из исходных документов репозитория.

Исходники остаются единственным источником правды: правим их, затем пересобираем.
    python3 docs/appendix/build_appendix.py
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.dirname(HERE)
OUT = os.path.join(HERE, "appendix.md")
REPO_URL = "https://github.com/AndrewStrizhevskiy/vpotoke-case3-tips"

HEADING = re.compile(r"^(#{1,6}) ")
LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)]+)\)")


def read(name):
    with open(os.path.join(DOCS, name), encoding="utf-8") as f:
        return f.read().split("\n")


def level(line):
    m = HEADING.match(line)
    return len(m.group(1)) if m else 0


def section(name, heading, subsections=True):
    """Тело раздела `heading` без самой строки заголовка.

    subsections=True — вместе с подразделами (до заголовка того же или старшего уровня),
    False — только вступление до первого любого заголовка.
    """
    lines = read(name)
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        raise SystemExit(f"{name}: нет заголовка «{heading}»")
    top = level(lines[start])
    body = []
    for line in lines[start + 1:]:
        lv = level(line)
        if lv and (lv <= top or not subsections):
            break
        body.append(line)
    return body


def embed(lines, shift):
    """Сдвинуть заголовки на `shift` уровней и поправить относительные ссылки (файл лежит на уровень ниже)."""
    out = []
    for line in lines:
        lv = level(line)
        if lv:
            line = "#" * min(lv + shift, 6) + line[lv:]
        out.append(LINK.sub(lambda m: "](../" + m.group(1) + ")", line))
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def part(title, *blocks):
    return "\n\n".join([title] + [b for b in blocks if b])


def build():
    a1 = part("### A.1. Схема системы",
              "![Схема системы](../../diagrams/system.png)",
              "**Блоки**", embed(section("a1-system-diagram.md", "## Блоки"), 2),
              "**Потоки данных**", embed(section("a1-system-diagram.md", "## Потоки"), 2))
    a2 = part("### A.2. ER-модель",
              "![ER-модель](../../diagrams/er.png)",
              "**Связи**", embed(section("a2-er-model.md", "## 3. Связи"), 2),
              "Атрибуты всех 7 сущностей с типами и ограничениями — [A.2 полностью](../a2-er-model.md).")
    a3 = part("### A.3. Схема состояний платежа",
              "![Жизненный цикл платежа](../../diagrams/states.png)",
              embed(section("a3-payment-states.md", "## Как читать"), 2),
              "**Что происходит с QR**", embed(section("a3-payment-states.md", "## Что происходит с QR"), 2))
    a4 = part("### A.4. Диалоговая форма и генерация QR",
              "![Диалоговая форма чаевых](../../diagrams/dialog.png)",
              "**Генерация QR и платёжной ссылки**",
              embed(section("06-qr-and-dialog.md", "## 3. Генерация QR и платёжной ссылки"), 2),
              "**Четыре экрана**",
              # картинка уже стоит в начале раздела, повторно её не вставляем
              embed(section("06-qr-and-dialog.md", "## 4. Диалоговая форма выбора чаевых (4 экрана)"), 2)
              .replace("![Диалоговая форма чаевых](../../diagrams/dialog.png)\n\n", ""),
              "Payload QR с объяснением полей и подпись — [документ полностью](../06-qr-and-dialog.md).")
    b1 = part("### B.1. Правила QR и платежа",
              "**Проверки QR**", embed(section("02-rules.md", "## Проверки QR"), 2),
              "**Правила платежа**", embed(section("02-rules.md", "## Правила платежа"), 2),
              "**Порядок выполнения**", embed(section("02-rules.md", "## Порядок выполнения"), 2))
    b2_parts = ["## Проверка QR", "## Суммы", "## Создание и жизненный цикл платежа",
                "## Платёжный шлюз", "## Запрос и доступ", "## Принципы"]
    b2 = part("### B.2. Матрица «ситуация → код ошибки → HTTP → retryable»",
              embed(section("b2-error-matrix.md", "# B.2. Матрица «ситуация → код ошибки → HTTP → retryable»",
                            subsections=False), 2),
              *[f"**{h[3:]}**\n\n" + embed(section("b2-error-matrix.md", h), 2) for h in b2_parts])
    b3 = part("### B.3. Тесты",
              embed(section("03-tests.md", "# Тесты — кейс 3 (заполненный шаблон 03_тесты)", subsections=False), 2),
              "**Ограничения прототипа**", embed(section("03-tests.md", "## Ограничения прототипа"), 2))
    b4 = part("### B.4. Архитектурные решения (ADR)",
              embed(section("README.md", "# Архитектурные решения — кейс 3 «Онлайн-чаевые»", subsections=False), 2))
    c = part("## C. Журнал промптов",
             embed(section("ai-journal.md", "# Журнал промптов (раздел C приложения)"), 1))
    return "\n\n".join([HEADER, "## A. Диаграммы", a1, a2, a3, a4, "## B. Таблицы", b1, b2, b3, b4,
                        c, CONTRIBUTION, ARTIFACTS, CHECKLIST]) + "\n"


HEADER = f"""# Приложение к презентации — кейс 3 «Онлайн-чаевые»

**Команда:** [название] · **Состав:** [ФИО ×4] · **Чемпионат:** ВПОТОКЕ, этап 1

**Репозиторий:** {REPO_URL}

> Документ собран скриптом `docs/appendix/build_appendix.py` из исходных документов репозитория.
> Исправления вносим в исходники и пересобираем, а не правим этот файл руками.
> Места в [квадратных скобках] заполняет команда.

**Содержание:** A. Диаграммы (A.1–A.4) · B. Таблицы (B.1–B.4) · C. Журнал промптов ·
D. Матрица вклада · E. Артефакты · F. Чек-лист"""

CONTRIBUTION = """## D. Матрица вклада

| Участник | Роль | Файлы / задачи | Часы | Подпись |
|-|-|-|-|-|
| [ФИО] | Архитектор | ADR-001…009, схемы A.1–A.4, диалоговая форма и генерация QR (06), API (05), шаблоны 07 и 11_ИБ, матрица B.2, слайды 3–5, ревью PR | [ч] | |
| [ФИО] | Аналитик | 02-rules, 02-roles, 03-data-dictionary, 04-requirements, 09-reports, скрипт проверки данных | [ч] | |
| [ФИО] | Разработчик | прототип `04_прототип.py` (платёж, `qr`, `report`), `run_tests.py` (16 тестов), 03-tests | [ч] | |
| [ФИО] | Презентатор | слайды 1, 2, 6–8, сборка приложения, экспорт в PDF, защита | [ч] | |

Часы и подписи каждый заполняет сам. Задачи у всех четырёх разные."""

ARTIFACTS = f"""## E. Артефакты

| Артефакт | Где взять | Статус |
|-|-|-|
| Ссылка на Git-ветку `team_N_case_3` | создать ветку от `main`, когда будет номер команды | [ ] |
| Скриншот последнего коммита | в финальной ветке: `python3 docs/screenshots/make_screenshots.py commit` | [ ] |
| Скриншот вывода прототипа, в том числе qr_001 и qr_003 | `tests.png` (тесты), `demo.png` (сквозной сценарий) | [x] |
| Скриншот `md5sum`: данные не менялись | `md5.png` | [x] |

**Тесты прототипа:** `python3 prototype/run_tests.py` (перед запуском `export TIPS_HMAC_SECRET=<демо-ключ из README>`)

![Тесты прототипа](../screenshots/tests.png)

**Исходные данные не менялись:** `cd prototype/01_данные && md5sum -c MD5SUMS`

![md5sum](../screenshots/md5.png)

**Сквозной сценарий:** `python3 prototype/demo.py` (qr_001 — успех, qr_003 — отказ, повтор, подделка подписи, таймаут, отмена, отчёт, аудит)

![Сквозной сценарий](../screenshots/demo.png)

[Скриншот последнего коммита вставить после создания ветки `team_N_case_3`.]

Скриншоты — вывод реальных команд, отрисованный скриптом `docs/screenshots/make_screenshots.py`.
Если страниц получится больше 15, уберите `demo.png`: тесты и `md5sum` закрывают пункты шаблона.

Репозиторий сейчас приватный: перед сдачей открыть доступ жюри или сделать публичным ({REPO_URL})."""

CHECKLIST = """## F. Чек-лист приложения

- [x] Схема системы: 5–6 блоков, потоки подписаны — A.1
- [x] Схема состояний платежа — A.3
- [x] Диалоговая форма (4 экрана) и генерация QR — A.4
- [x] ER-модель — A.2
- [x] Правила Q-1…Q-6, P-1…P-4 с обоснованием — B.1
- [x] Матрица «ситуация → код → HTTP → retryable» — B.2
- [x] Тесты T3-1…T3-5 с фактом — B.3 (16 тестов, все пройдены)
- [x] Журнал промптов: 5+ строк — C
- [ ] Матрица вклада: 4 человека, задачи разные — D, заполнить ФИО и часы
- [ ] Git-ветка и коммит — E
- [ ] Экспорт в PDF до защиты"""


if __name__ == "__main__":
    text = build()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"{os.path.relpath(OUT)}: {len(text.splitlines())} строк, {len(text)} символов")
