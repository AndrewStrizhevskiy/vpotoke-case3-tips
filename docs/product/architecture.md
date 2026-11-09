# Продукт «Чаевые»: из прототипа в сервис

Проект дизайна для ветки `product/foundation`. Составлен архитектором (Claude, роль «architect») 2026-11-09 по материалам репозитория; решения, помеченные как «решение владельца», принимает команда. Исходные ADR-001…009 остаются описанием хакатонного прототипа: там, где ниже сказано «ошибка ADR», действует этот документ.

Исходные условия: эмулятор шлюза остаётся, но за интерфейсом, который позже заменяется реальным провайдером; разработка идёт силами одного разработчика.

## 1. Стек

Python 3.12, FastAPI, Pydantic v2, PostgreSQL 16, SQLAlchemy 2.0, Alembic, psycopg 3, Redis (только для rate limit), PyJWT, argon2-cffi.

- Python 3.9 (локально сейчас) вышел из поддержки в октябре 2025, CI идёт на 3.12. Код пишется совместимым с 3.9, пока локальный интерпретатор не обновлён.
- Слой БД синхронный: эндпоинты `def`, транзакции и `FOR UPDATE` проще писать и тестировать. Нагрузка небольшая. Async при необходимости затрагивает только адаптеры.
- Фоновые задачи — отдельный процесс-воркер без Celery: цикл и `SELECT … FOR UPDATE SKIP LOCKED`.

## 2. Раскладка пакета

```
src/tips/
  domain/errors.py       ERRORS, PaymentError
  domain/money.py        CENT, MAX_*, to_money, calc_amounts, fmt
  domain/qr_sign.py      compute_signature, verify_signature, check_exp, parse_time, TIME_FMT, QR_TTL
  domain/validation.py   validate_request, validate_qr_request, parse_day
  domain/lifecycle.py    статусы, таблица переходов и повторов ADR-002 (чистые функции)
  domain/reports.py      report_metrics, round_money
  ports.py               Protocol: PaymentRepo, QrRepo, ReceiptRepo, MerchantRepo, EmployeeRepo,
                         AuditLog, UnitOfWork, Clock, PaymentGateway, SecretProvider
  app/payments.py        create_payment, confirm_payment, cancel_payment, find_payment
  app/qr.py              create_qr (без PNG)
  app/reports.py         build_report
  jobs/reconcile.py      reconcile; позже auto_cancel
  adapters/csv_store.py  Store (целиком) и чтение seed-CSV
  adapters/gateway/emulator.py   emulate_gateway, load_scenarios
  adapters/qr_png.py     qrencode
  adapters/sql/          models, repos, uow (этап M2)
  config.py              get_secret, позже pydantic-settings
  cli/main.py            main, emit, payment_json, money_json, exit_code
  http/                  (этап M3) app, routers, schemas, auth, rate limit
```

`prototype/04_прототип.py` остаётся тонкой обёрткой: `run_tests.py` запускает его с `python -I`, а `contract_check.py` загружает файл через `exec_module` и читает `proto.ERRORS`. Сервисы в `app/*` получают репозитории, шлюз и `now` аргументами и не работают с `os`/`csv`.

## 3. Схема PostgreSQL

Деньги `NUMERIC(12,2)`, время `timestamptz`, статусы `text CHECK (…)`.

- `merchants(merchant_id PK, name, status CHECK IN (ACTIVE, BLOCKED, PENDING))`.
- `employees(employee_id PK, merchant_id FK, name, payout_mask CHECK ~ '^\*{4}\d{4}$')`, `UNIQUE(employee_id, merchant_id)` как цель составного FK.
- `receipts(merchant_id, receipt_id, amount CHECK 0 < amount <= 1000000, currency, PK(merchant_id, receipt_id))`.
- `qr_codes(qr_id PK, merchant_id, employee_id, receipt_id, nonce, exp, signature, key_id, created_at)`: составной `FK(employee_id, merchant_id)` (официант работает у этого мерчанта), `FK(merchant_id, receipt_id)`, `CHECK(exp <= created_at + 24 h)`.
- `payments`: `payment_id PK`, `qr_id FK`, `merchant_id`, `employee_id`, `amount`, `tip`, `amount_total`, `currency`, `status`, `error_code`, `idempotency_key uuid`, `request_hash`, `confirm_token_hash`, `provider`, `provider_payment_id`, `created_at`, `submitted_at`, `finalized_at`, `version`.
  - `CHECK(tip >= 0 AND tip <= amount * 0.5 AND amount_total = amount + tip AND amount_total <= 2000000)`.
  - `UNIQUE(qr_id, idempotency_key)`.
  - `UNIQUE INDEX one_success ON payments(qr_id) WHERE status = 'SUCCESS'` (nonce гасится на SUCCESS).
  - `UNIQUE INDEX one_active ON payments(qr_id) WHERE status IN ('CREATED','PENDING')`: блокировка QR из ADR-002 на уровне БД. Без неё два одновременных запроса с разными ключами создадут два CREATED.
  - `UNIQUE(provider, provider_payment_id)`; индексы по `(merchant_id, created_at)`, `(employee_id, created_at)`, частичный `(status, submitted_at)` для воркера.
- `audit_events(event_id bigserial PK, payment_id FK, qr_id, action, from_status, to_status, amount_total, actor, at)`: только дописывается (`REVOKE UPDATE, DELETE` у роли приложения плюс триггер).
- `gateway_events(provider, event_id, payload jsonb, received_at, processed_at, PK(provider, event_id))`: входящий ящик вебхуков, дедупликация.
- `users(id, login, password_hash, role, merchant_id, employee_id, status)`, `refresh_tokens(id, user_id, token_hash, expires_at, revoked_at)`.
- Таблицы Report нет: отчёт считается из `payments` (ADR-007).

Транзакции:
- **Создание:** `SELECT … FROM qr_codes WHERE qr_id = … FOR UPDATE` → поиск по ключу → проверки → `INSERT payment` + `INSERT audit` → `COMMIT`. Нарушение уникальности переводится в код: `(qr_id, key)` — ответ как на повтор, `one_active` — `PAYMENT_IN_PROGRESS`, `one_success` — `QR_USED`.
- **Переход статуса:** `UPDATE payments SET status = :new, version = version + 1 WHERE payment_id = :id AND status = :expected RETURNING …` и аудит в той же транзакции. 0 строк — перечитать и ответить по таблице повторов.
- **Вызов шлюза вне транзакции:** tx1 CREATED→PENDING и коммит, затем вызов шлюза с `idempotency_key = payment_id`, затем tx2 записывает итог условным UPDATE.

## 4. Ошибки и пробелы ADR, которые нужно исправить

Небезопасно для продакшена:

1. **Сверка PENDING → DECLINED через 15 минут без вопроса шлюзу (ADR-001/002).** Если банк всё же списал деньги, клиент заплатил, QR свободен, возможна вторая оплата. Сверка должна вызывать `gateway.get_status()`, закрывать платёж DECLINED только при подтверждённом отказе или когда шлюз платёж не знает, иначе эскалировать. Сетевая ошибка при `charge` — всегда PENDING. Нужна политика «позднего успеха» (вебхук SUCCESS после DECLINED/CANCELED). *В прототипе правило остаётся учебным.*
2. **`confirm_token` нельзя вернуть повторно (ADR-003):** хранится только SHA-256. При повторе POST /tips с тем же ключом и тем же `request_hash` выпускается новый токен и заменяет хеш. `idempotency_key` становится данными «на предъявителя»: строго UUIDv4 на сервере, не пишется в логи.
3. **`confirm_token` в query у `GET /payments/{id}` утекает в логи и Referer.** Переносится в заголовок `X-Payment-Token`; проверка `hmac.compare_digest(sha256(token), stored)` в HTTP-зависимости, домен о токене не знает.
4. **Nonce из 6 hex с UNIQUE:** коллизии примерно с ~4 тыс. QR. Для новых QR не менее 64 бит, UNIQUE по nonce снимается.
5. **ID в контракте ограничены и последовательны** (`^p_[0-9]{4}$`, `^qr_[0-9]{3}$`): потолок и перебор. Нужны непрозрачные ID (префикс + ULID/UUIDv7).
6. **Деньги в JSON как float** (`money_json`, `round_money`): в API v2 строки-десятичные.
7. **Подпись обрезана до 64 бит, `kid` нет.** Для seed-данных допустимо; для новых QR payload v2 с `v`, `kid` и 128 битами.
8. **Блок по IP после 5 неверных подписей:** за NAT ресторана один злоумышленник блокирует всех гостей. Лимит по связке IP + `qr_id`, скользящее окно.
9. **Автоотмена CREATED при hosted-форме:** клиент мог уже заплатить на странице провайдера. Отмена (ручная и автоматическая) сначала делает `gateway.cancel()` (void), затем меняет статус; окно автоотмены больше 15 минут из-за 3DS.

Не реализовано в прототипе:
- **JWT:** подпись ES256/EdDSA с `kid`; access 15 минут, refresh хранится хешем и ротируется; claims `sub`, `role`, `merchant_id`, `employee_id`, `iss`, `aud`, `exp`, `jti`; алгоритмы в `decode` фиксируются явно.
- **RBAC:** матрица прав из `docs/11-security.md` одной таблицей политик; владелец ресурса берётся из claims, а не из параметров запроса.
- **Rate limit:** грубые лимиты на границе (nginx/WAF) плюс библиотека `limits` с Redis; 429 `RATE_LIMITED` и `Retry-After`.
- **Секреты:** интерфейс `SecretProvider` (dev — env, прод — secret manager), HMAC-ключи как keyring `{kid: key}`: подписываем активным, проверяем всеми действующими. Маскирование логов фильтром.
- **Воркер:** `auto_cancel` и `reconcile` пачками через `SKIP LOCKED`; время отправки берётся из `payments.submitted_at`, а не из скана аудита.

## 5. Интерфейс платёжного шлюза

```python
class PaymentGateway(Protocol):
    def charge(self, req: ChargeRequest) -> ChargeResult: ...
    #  req: payment_id (он же ключ идемпотентности у провайдера), amount: Decimal, currency,
    #       payment_method_token | None (одноразовый токен из hosted fields), return_url, metadata
    #  result: status in {SUCCEEDED, FAILED, PENDING, ACTION_REQUIRED}, provider_payment_id,
    #          error_code in {CARD_DECLINED, GATEWAY_TIMEOUT, ...}, action_url
    def get_status(self, provider_payment_id: str) -> ChargeResult: ...
    def cancel(self, provider_payment_id: str) -> ChargeResult: ...   # void до списания
    def parse_webhook(self, headers, body: bytes) -> GatewayEvent: ...  # проверяет подпись
```

Эмулятор игнорирует токен и считает исход по `metadata` через `emulate_gateway`. Реальный провайдер: карту клиент вводит в iframe провайдера, на сервер приходит только токен (PCI SAQ A); на странице оплаты строгий CSP и SRI. Контракт расширяется полем `payment_method_token` и ответом 202 с `action_url` (3DS). Вебхук: `INSERT` в `gateway_events` (конфликт — уже обработано), затем условный UPDATE статуса. Один набор контрактных тестов гоняется на эмуляторе и на sandbox провайдера.

## 6. Этапы и критерии

| Этап | Что | Критерий приёмки |
|-|-|-|
| M0 | `pyproject`, ruff, pytest-обёртка над `run_tests.py` и `contract_check.py`, CI на ветке | CI зелёный на неизменённом коде |
| M1 | Рефакторинг без изменения поведения, CSV за портами | 18 тестов, `contract_check`, `demo`, MD5 зелёные; эталонные выводы CLI побайтно совпадают; у `domain/*` юнит-тесты без ввода-вывода |
| M2 | SQL-адаптер и Alembic | те же приёмочные тесты в режиме `TIPS_STORE=csv\|sql`; 20 параллельных create с разными ключами на один QR дают ровно 1 CREATED; параллельный confirm вызывает шлюз один раз; UPDATE/DELETE в `audit_events` падают |
| M3 | HTTP API | коды 201/200/202/402, `confirm_token`, перевод ошибок в HTTP; T3 портированы на `TestClient`; контракт проверен по всем 10 путям |
| M4 | Auth и RBAC | матрица «роль × эндпоинт»; чужой `employee_id` даёт 403; просроченный или чужой JWT даёт 401 |
| M5 | Воркер, rate limit, keyring и ротация, маскирование логов | ротация без простоя; 429 с `Retry-After`; в логах нет sig, nonce, токенов |
| M6 | Адаптер реального провайдера (sandbox), вебхуки, фронт с hosted fields | E2E: успех, отказ, 3DS, таймаут, повтор вебхука, поздний успех |
| M7 | Эксплуатация | метрики и алерты на зависшие PENDING, бэкапы с PITR, пентест, нагрузочный тест |

## 7. Решения владельца (не архитектора)

1. **Юридическая схема денег:** кто держит средства (сплит и выплаты у провайдера, эквайринг мерчанта или агрегатор по 161-ФЗ), чеки по 54-ФЗ, налоги официантов. От этого зависит выбор провайдера.
2. **Провайдер:** hosted fields, void, `get_status`, подписанные вебхуки, выплаты физлицам.
3. **Источник реестра чеков (ADR-003):** с какими POS интегрироваться (iiko, r_keeper и т. д.). Без этого нет «серверной правды» о сумме.
4. **API v2:** ломать контракт сейчас (непрозрачные ID, деньги строками, токен в заголовке, payload QR v2) или держать совместимость с тестовыми векторами хакатона как legacy-режим.
5. **Поздний успех и двойная оплата:** автовозврат или ручной разбор, кто платит за возвраты; хостинг в РФ (152-ФЗ), целевой SLA.

Допущения, которые не проверялись: нагрузка небольшая, один регион, у провайдера есть токенизация и void.
