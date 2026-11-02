# ER-модель — кейс 3 «Онлайн-чаевые»

## Как заполнять

Дана одна сущность (Merchant) как образец и связи. Заполни остальные 5 сущностей,
их атрибуты и связи. Для каждой сущности укажи PK/FK, типы, обязательность и ограничения.

## 1\. Сущности (6)

|Сущность|Что хранит|Зачем|
|-|-|-|
|Merchant|merchant\_id, name, status, description|справочник мерчантов|
|Employee|?|?|
|QR|?|?|
|Payment|?|?|
|Report|?|?|
|AuditEvent|?|?|

**Подсказки:**

* Employee — сотрудник (официант), привязан к мерчанту.
* QR — одноразовая ссылка на оплату с подписью.
* Payment — факт оплаты (чек + чаевые).
* Report — агрегаты для отчётности.
* AuditEvent — журнал событий для разбора.

## 2\. Атрибуты (заполни)

### 2.1. Merchant (ОБРАЗЕЦ — не трогать)

|Поле|Тип|PK/FK|Обяз.|Ограничение|
|-|-|-|-|-|
|merchant\_id|string|**PK**|да|`m\_NNN`|
|name|string|—|да|1–100|
|status|enum|—|да|ACTIVE/BLOCKED/PENDING|
|description|string|—|нет|0–200|

### 2.2. Employee (заполни)

|Поле|Тип|PK/FK|Обяз.|Ограничение|
|-|-|-|-|-|
|employee\_id|?|?|?|?|
|merchant\_id|?|?|?|?|
|name|?|?|?|?|
|payment\_details|?|?|?|?|

**Подсказки:**

* `employee\_id` — `e\_NNN`.
* `merchant\_id` — FK → Merchant.
* `payment\_details` — только маска `\*\*\*\*NNNN`, не оригинал.

### 2.3. QR (заполни)

|Поле|Тип|PK/FK|Обяз.|Ограничение|
|-|-|-|-|-|
|qr\_id|?|?|?|?|
|merchant\_id|?|?|?|?|
|employee\_id|?|?|?|?|
|receipt\_id|?|?|?|?|
|nonce|?|?|?|?|
|exp|?|?|?|?|
|signature|?|?|?|?|

**Подсказки:**

* `qr\_id` — `qr\_NNN`.
* `receipt\_id` — `r\_NNNN`, уникален для мерчанта.
* `nonce` — 6 hex, одноразовый.
* `exp` — ISO 8601, TTL 24 ч.
* `signature` — HMAC-SHA256, 16 hex.

### 2.4. Payment (заполни)

|Поле|Тип|PK/FK|Обяз.|Ограничение|
|-|-|-|-|-|
|payment\_id|?|?|?|?|
|qr\_id|?|?|?|?|
|amount|?|?|?|?|
|tip|?|?|?|?|
|status|?|?|?|?|
|timestamp|?|?|?|?|

**Подсказки:**

* `payment\_id` — `p\_NNNN`.
* `amount` — > 0, ≤ 1 000 000.
* `tip` — ≥ 0, ≤ amount × 0.5.
* `status` — CREATED/PENDING/SUCCESS/DECLINED/CANCELED.

### 2.5. Report (заполни)

|Поле|Тип|PK/FK|Обяз.|Ограничение|
|-|-|-|-|-|
|report\_id|?|?|?|?|
|merchant\_id|?|?|?|?|
|period|?|?|?|?|
|tips\_total|?|?|?|?|

**Подсказки:**

* `report\_id` — `rep\_NNNN`.
* `period` — `YYYY-MM`.
* `tips\_total` — ≥ 0.

### 2.6. AuditEvent (заполни)

|Поле|Тип|PK/FK|Обяз.|Ограничение|
|-|-|-|-|-|
|event\_id|?|?|?|?|
|payment\_id|?|?|?|?|
|action|?|?|?|?|
|from\_status|?|?|?|?|
|to\_status|?|?|?|?|
|actor|?|?|?|?|
|at|?|?|?|?|

**Подсказки:**

* `event\_id` — `evt\_NNNN`.
* `action` — payment\_created / payment\_confirmed / payment\_declined / payment\_canceled.
* `actor` — system / client / officer.
* `at` — ISO 8601.

## 3\. Связи (заполни)

|Связь|Тип|Обоснование|
|-|-|-|
|Merchant → Employee|?|?|
|Merchant → QR|?|?|
|Employee → QR|?|?|
|QR → Payment|?|?|
|Merchant → Report|?|?|
|Payment → AuditEvent|?|?|

**Подсказки:**

* 1:N — у мерчанта много официантов, у официанта много QR и т.д.
* 1:N — по одному QR может быть несколько попыток платежа.
* 1:N — каждый переход статуса — отдельное событие аудита.

## 4\. Диаграмма (draw.io)

Нарисуйте 6 прямоугольников (сущности) с полями PK/FK и соедините стрелками
с подписями `1` и `N`. Экспортируйте в PNG и вставьте в приложение (раздел A.2).

## 5\. Критерий приёмки

* \[ ] 6 сущностей описаны
* \[ ] Для каждой сущности указаны PK/FK, типы и ограничения
* \[ ] Связи описаны с типом (1:N, 1:1) и обоснованием
* \[ ] `payment\_details` — только маска `\*\*\*\*NNNN`
* \[ ] `signature` — HMAC-SHA256, не plain hash
* \[ ] Диаграмма в draw.io с PK/FK и связями

