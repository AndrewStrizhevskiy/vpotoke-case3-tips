# Кейс 3 «Онлайн-чаевые» — ВПОТОКЕ

[![tests](https://github.com/AndrewStrizhevskiy/vpotoke-case3-tips/actions/workflows/ci.yml/badge.svg)](https://github.com/AndrewStrizhevskiy/vpotoke-case3-tips/actions/workflows/ci.yml)

Команда № 11 «Los Pollos Hermanos». Ветка для сдачи хакатона — `team_11_case_3`; в `main` развитие проекта: HTTP-сервис, PostgreSQL, браузерная форма.

Платформа онлайн-чаевых для сети «Вкус-Сервис»: гость сканирует QR на чеке, выбирает чаевые и платит;
платформа ведёт мерчантов, официантов и платежи и строит отчёты. Решение: архитектура с обоснованием каждого
спорного места кейса, спецификация API, консольный прототип с эмулятором банка и 18 автотестов, включая сверку ответов с OpenAPI.

## Результат

| Что | Где |
|-|-|
| Презентация, 8 слайдов | [`submission/presentation.pdf`](submission/presentation.pdf) |
| Приложение, 13 страниц | [`submission/appendix.pdf`](submission/appendix.pdf) |
| Архитектурные решения (9 ADR) | [`docs/README.md`](docs/README.md) |
| Схемы: система, состояния платежа, ER-модель, диалог | [`diagrams/`](diagrams/) |
| API (OpenAPI) | [`api/05_api_schemas.yaml`](api/05_api_schemas.yaml) (v1), [`api/v2.yaml`](api/v2.yaml) (v2) |
| Продуктовая версия: архитектура, эксплуатация | [`docs/product/architecture.md`](docs/product/architecture.md), [`docs/product/operations.md`](docs/product/operations.md) |
| Прототип и тесты | [`prototype/`](prototype/), результаты — [`docs/03-tests.md`](docs/03-tests.md) |
| Журнал использования ИИ | [`docs/ai-journal.md`](docs/ai-journal.md) |

## Продуктовая версия

Поверх консольного прототипа сделан сервис, который можно запускать и проверять:

- HTTP-сервис на FastAPI (`src/tips/http`): API v1 и v2, вход по JWT и роли, ограничение частоты запросов;
- хранилище PostgreSQL с миграциями Alembic (`migrations/`) и защитой от двойной оплаты в самой базе;
- QR версии 2, адаптер платёжного провайдера с тестовым банком (песочница), вебхуки, возвраты, фоновая сверка (`python -m tips.jobs.worker`);
- браузерная форма чаевых `web/pay` (четыре экрана макета);
- развёртывание в Docker (`Dockerfile`, `docker-compose.yml`), резервные копии (`deploy/`), метрики `/metrics`.

Реального платёжного провайдера и интеграции с кассой нет: в production оплата картой выключена.
Как запустить локально, настройки и порядок развёртывания: [`docs/product/architecture.md`](docs/product/architecture.md) и
[`docs/product/operations.md`](docs/product/operations.md). Проверки: `pip install -e ".[dev]"`, затем
`TIPS_HMAC_SECRET=demo-secret-k3 python -m unittest discover -s tests -p "test_*.py"` (PostgreSQL для тестов поднимается сам через `pgserver`, если поставить `.[local-db]`).

## Запуск прототипа

Нужен Python 3 (проверено на 3.12), сам прототип без сторонних библиотек. Для теста T3-14 (сверка с OpenAPI) нужно `pip install -r prototype/requirements-dev.txt`, без них он пропускается. Ключ подписи берётся из окружения;
`demo-secret-k3` — демо-ключ организаторов, которым подписаны данные кейса.

```bash
export TIPS_HMAC_SECRET=demo-secret-k3
python3 prototype/run_tests.py       # 18 тестов, ожидается «18 из 18»
python3 prototype/demo.py            # сквозной сценарий: QR → оплата → повтор → отказ → отмена → отчёт
```

Отдельные команды прототипа (время зафиксировано, чтобы данные кейса оставались действительными):

```bash
python3 prototype/04_прототип.py --now 2025-11-01T18:00:00Z qr req_qr.json      # выпуск QR на чек
python3 prototype/04_прототип.py --now 2025-11-01T18:00:00Z pay req_pay.json    # создать и подтвердить платёж
python3 prototype/04_прототип.py report --merchant m_001 --from 2025-11-01 --to 2025-11-30
```

Исходные CSV не меняются: `cd prototype/01_данные && md5sum -c MD5SUMS`.

## Структура репозитория

```
case/          материалы организаторов (не редактируются)
prototype/     консольный прототип, тесты, демо; исходные данные кейса в 01_данные/
api/           спецификации OpenAPI (v1 и v2)
src/tips/      сервис: domain, app, adapters, http, jobs, cli
migrations/    миграции PostgreSQL (Alembic)
web/pay/       браузерная форма чаевых
deploy/        резервные копии, пример окружения; рядом Dockerfile и docker-compose.yml
tests/         автотесты сервиса
data/          таблица сценариев эмулятора банка
diagrams/      схемы (PNG, SVG, draw.io) и скрипты их генерации
docs/          решения, спецификации, отчёты, руководства команды, скриншоты, журнал ИИ
submission/    сдача: презентация и приложение (PDF), исходники слайдов и скрипты сборки
```

## Команда

| Участник | Роль | Основные материалы |
|-|-|-|
| Стрижевский Андрей | архитектор | ADR, схемы, API, безопасность, матрица ошибок, QR и отчёты в прототипе |
| Парамонов Данил | аналитик | правила, роли, словарь данных, требования, отчёты |
| Верховых Егор | разработчик | прототип и тесты |
| Левенко Богдан | презентатор | презентация и приложение |

## Правила работы

- Изменения вносятся через pull request в `main`; ветка сдачи `team_11_case_3` обновляется после влития.
- Секреты в репозиторий не попадают: ключ подписи только в `TIPS_HMAC_SECRET`.
- ИИ — ассистент, а не автор: результаты проверяются запуском или сверкой с источником, использование записано в журнале.
- Руководства по ролям: [`docs/guides/`](docs/guides/). Пересборка PDF: [`submission/deck/README.md`](submission/deck/README.md)
  (презентация) и `python3 docs/appendix/make_pdf.py` (приложение).
