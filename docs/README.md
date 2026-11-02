# Архитектурные решения — кейс 3 «Онлайн-чаевые»

Черновики архитектора от 2026-11-02. Каждый ADR закрывает противоречие в материалах кейса
или пробел в них. Перед сдачей команда перерабатывает тексты и записывает итерации в журнал промптов.

| ADR | Решение | Куда идёт |
|-|-|-|
| [001](adr-001-gateway-emulator.md) | Эмулятор: таблица сценариев + формула организаторов | 07, слайд 4, тесты T3-1/T3-2 |
| [002](adr-002-payment-lifecycle.md) | Состояния, переходы, отмена только из CREATED, nonce гасится на SUCCESS | 07, A.3, T3-5 |
| [003](adr-003-trust-boundary.md) | Сумма чека из реестра, подпись в ссылке | A.1, 11_ИБ (U-5), слайд 5 |
| [004](adr-004-validation-order.md) | 8 шагов проверок, лимиты, Decimal | 02_правила, B.1, слайд 4 |
| [005](adr-005-data-model.md) | ER и CSV, денормализация, Receipt | 08 (ER), A.2, 03_data_dictionary |
| [006](adr-006-error-catalog.md) | Каталог ошибок и экраны клиента | B.2, 05_api_schemas, 06 (экран 4) |
| [007](adr-007-reporting.md) | Метрики отчётов и контрольные значения | 09_reports |
| [008](adr-008-idempotency.md) | Идемпотентность и атомарность | 02_правила (P-2), T3-4 |
| [009](adr-009-secrets-time-ttl.md) | Секреты, UTC, `--now`, TTL, rate limit, маскирование логов | 11_ИБ, 04_requirements (NFR) |

## Новые коды ошибок (нет в материалах кейса)

`AMOUNT_MISMATCH`, `IDEMPOTENCY_CONFLICT`, `PAYMENT_IN_PROGRESS`, `PAYMENT_STATE_CONFLICT`,
`GATEWAY_TIMEOUT`, `NOT_FOUND`, `VALIDATION_ERROR`, `ACCESS_DENIED`, `RATE_LIMITED`.

## Что нужно от разработчика (прототип)

1. `emulate_gateway()` — по ADR-001 с `data/gateway_scenarios.csv`.
2. `SECRET` из `TIPS_HMAC_SECRET`, `check_exp(qr, now)` с `--now` (ADR-009).
3. `calc_amounts()` на `Decimal` с `ROUND_HALF_UP` (ADR-004).
4. Идемпотентность по `idempotency_key` и последовательный `payment_id` с `p_0101` (ADR-008).
5. Колонки `payments_out.csv` по ADR-005.
