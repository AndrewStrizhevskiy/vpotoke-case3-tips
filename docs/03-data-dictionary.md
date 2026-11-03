# Словарь данных — кейс 3

Заполненная копия [шаблона](../case/03_data_dictionary.md). Основания: [A.2](a2-er-model.md), [ADR-005](adr-005-data-model.md); каталог кодов — [B.2](b2-error-matrix.md), а не `08_errors.md` (там ER-шаблон). Все примеры основных таблиц взяты из первой строки соответствующего исходного CSV.

## Четыре исходных CSV

UTF-8, заголовок в первой строке, разделитель — запятая. Идентификаторы — строки, деньги — десятичные числа с двумя знаками; float для расчётов запрещён. Обязательное поле не пустое. Уникальность имён и названий не требуется. Всего **22 колонки: 4 + 4 + 7 + 7**.

### merchants.csv

| Поле | Тип | Обяз. | Ограничение | Пример из CSV | Код ошибки |
|---|---|---|---|---|---|
| merchant_id | string | да | PK, m_NNN, уникален | `m_001` | VALIDATION_ERROR |
| name | string | да | 1–100 символов, не уникально | `Вкус-Сервис Тверская` | VALIDATION_ERROR |
| status | enum | да | ACTIVE / BLOCKED / PENDING; оплата только ACTIVE | `ACTIVE` | MERCHANT_BLOCKED |
| description | string | нет | 0–200 символов | `Ресторан сети, Тверская` | VALIDATION_ERROR |

### employees.csv

| Поле | Тип | Обяз. | Ограничение | Пример из CSV | Код ошибки |
|---|---|---|---|---|---|
| employee_id | string | да | PK, e_NNN, уникален | `e_001` | VALIDATION_ERROR |
| merchant_id | string | да | FK → merchants.merchant_id, m_NNN | `m_001` | VALIDATION_ERROR |
| name | string | да | 1–100 символов, не уникально | `Иванов И.` | VALIDATION_ERROR |
| payment_details | string | да | Только маска: четыре звёздочки и четыре цифры; не полный номер | `****1001` | VALIDATION_ERROR |

### payments.csv

| Поле | Тип | Обяз. | Ограничение | Пример из CSV | Код ошибки |
|---|---|---|---|---|---|
| payment_id | string | да | PK, p_NNNN, уникален | `p_0001` | VALIDATION_ERROR |
| merchant_id | string | да | FK → Merchant; совпадает с мерчантом QR | `m_001` | VALIDATION_ERROR |
| employee_id | string | да | FK → Employee; совпадает с сотрудником QR | `e_001` | VALIDATION_ERROR |
| amount | decimal(2) | да | 0 < amount ≤ 1 000 000; равен сумме чека в POS | `637.00` | AMOUNT_OUT_OF_RANGE; AMOUNT_MISMATCH |
| tip | decimal(2) | да | 0 ≤ tip ≤ amount × 0.5; ROUND_HALF_UP | `31.85` | TIP_OUT_OF_RANGE |
| status | enum | да | CREATED / PENDING / SUCCESS / DECLINED / CANCELED | `SUCCESS` | VALIDATION_ERROR |
| timestamp | datetime | да | ISO 8601 UTC, суффикс Z | `2025-11-01T18:00:00Z` | VALIDATION_ERROR |

### qr_links.csv

| Поле | Тип | Обяз. | Ограничение | Пример из CSV | Код ошибки |
|---|---|---|---|---|---|
| qr_id | string | да | PK, qr_NNN, уникален | `qr_001` | VALIDATION_ERROR |
| merchant_id | string | да | FK → Merchant; ACTIVE при выпуске и оплате | `m_001` | MERCHANT_BLOCKED |
| employee_id | string | да | FK → Employee; employee.merchant_id = qr.merchant_id | `e_001` | VALIDATION_ERROR |
| receipt_id | string | да | FK → Receipt по (merchant_id, receipt_id), r_NNNN; в запросе совпадает с QR | `r_1001` | RECEIPT_MISMATCH |
| nonce | string | да | UK, 6 hex; погашается только на SUCCESS | `c82561` | QR_USED |
| exp | datetime | да | ISO 8601 UTC; exp ≥ now при использовании; exp ≤ created_at + 24 ч при выпуске | `2025-11-01T23:59:00Z` | QR_EXPIRED |
| signature | string | да | Первые 16 hex HMAC-SHA256 от merchant_id\|employee_id\|receipt_id\|nonce\|exp | `68d0f361ac38c12b` | INVALID_QR |

## Расширение payments_out.csv

Проектный формат по ADR-005: семь колонок payments.csv **в исходном порядке**, затем следующие четыре. Прототип пишет `payments_out.csv` именно в этом формате, все 11 колонок (`prototype/04_прототип.py`, `OUT_FIELDS`). В запросе на создание платежа `receipt_id` обязателен и сверяется с QR (Q-5, тест T3-3в). Эти поля отсутствуют в seed; примеры ниже иллюстративные, не выдаются за значения исходных CSV.

| Поле | Тип | Обяз. | Ограничение | Проектный пример | Код ошибки |
|---|---|---|---|---|---|
| qr_id | string | да | FK → QR, явная связь нового платежа | qr_001 | INVALID_QR |
| idempotency_key | string | да | UUID v4; UK вместе с qr_id; одинаковое тело возвращает прежний платёж | 3f1c2a9e-7b4d-4e1a-9c2f-5d8e6b7a1c00 | VALIDATION_ERROR / IDEMPOTENCY_CONFLICT |
| amount_total | decimal(2) | да | amount + tip ≤ 2 000 000 | 668.85 | AMOUNT_OUT_OF_RANGE |
| error_code | string | нет | Код из B.2; пусто при отсутствии ошибки | CARD_DECLINED | VALIDATION_ERROR для неизвестного кода |

Новые payment_id начинаются с p_0101. Хранилище идемпотентности дополнительно содержит request_hash и created_at (ADR-008), но это не колонки исходного CSV. Ошибки формата дают 400 VALIDATION_ERROR; ошибки бизнес-проверок — HTTP из B.2. Пропавшие объекты и нарушение доступа обрабатываются отдельно как NOT_FOUND / ACCESS_DENIED. Реквизиты и ключи никогда не возвращаются в details ошибки.

## Связи и производные сущности

| Сущность | Ключи и ограничения | Хранение / источник |
|---|---|---|
| Merchant | PK merchant_id; 1:N Employee, QR, Report | merchants.csv |
| Employee | PK employee_id; FK merchant_id; 1:N QR | employees.csv; токен провайдера выплат в отдельном защищённом хранилище, в seed его нет |
| QR | PK qr_id, UK nonce; FK merchant_id, employee_id, составной FK на Receipt | qr_links.csv; один активный платёж CREATED/PENDING, максимум один SUCCESS |
| Payment | PK payment_id; FK qr_id, merchant_id, employee_id; UK (qr_id, idempotency_key) | payments.csv и проектное расширение payments_out.csv |
| Receipt | PK (merchant_id, receipt_id), amount > 0 и ≤ 1 000 000, currency = RUB | Внешний POS; для прототипа сумма восстанавливается из payments.csv |
| Report | PK report_id (rep_NNNN), UK (merchant_id, period YYYY-MM); tips_total ≥ 0, payments_count ≥ 0, avg_tip_pct 0–50 или null, success_rate 0–1 | Пересчитываемый агрегат из Payment, см. [отчёты](09-reports.md) |
| AuditEvent | PK event_id (evt_NNNN), FK payment_id и qr_id | Только добавление; девять полей по A.2 |

AuditEvent: event_id, payment_id, qr_id, action, from_status, to_status, amount_total, actor, at. from_status может быть null только при создании; actor = client/system/officer; at — UTC. action = payment_created/payment_submitted/payment_confirmed/payment_declined/payment_canceled. Переход и аудит атомарны, ручная смена статуса администратором не предусмотрена.

Для seed связь восстанавливается **по числовому суффиксу**: p_0001 → qr_001, …, p_0100 → qr_100; мерчант, сотрудник и день timestamp/exp сверяются. Это правило только для предоставленных 100 исторических платежей, не алгоритм назначения QR новым платежам. receipt_id уникален в пределах мерчанта в реестре чеков, однако один Receipt может иметь несколько последовательно выпущенных QR. Погашение nonce выводится из наличия SUCCESS, исходный QR не редактируется.

created_at QR отсутствует в seed: можно проверить формат и дату exp, но доказать ограничение TTL по одним CSV нельзя. Активность доступа сотрудника и токен провайдера выплат также не представлены в seed. Не добавляем вымышленные колонки.

## Критерии приёмки документа

- [x] Описаны четыре файла и все 22 исходные колонки.
- [x] У каждого поля есть тип, обязательность и ограничение; примеры сверены с CSV.
- [x] Коды соответствуют B.2; исправлена ссылка на каталог ошибок.
- [x] Реквизиты — только маска `****NNNN`.
- [x] Четыре дополнительные колонки отделены от seed, перечислены сущности A.2 и связи.
