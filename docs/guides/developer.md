# Руководство разработчика

Твои файлы: `prototype/04_прототип.py`, заполненный `03_тесты.md` (копия в `docs/03-tests.md`)
и скриншоты для приложения (раздел E). Прототип даёт бонусные баллы, и на защите тебя могут
попросить объяснить любую строку, так что пиши код, который сможешь объяснить.

## Что сейчас не так с прототипом организаторов

| Проблема | Почему важно | Решение |
|-|-|-|
| `emulate_gateway()` по формуле даёт qr_001 → h = 9 (TIMEOUT), qr_003 → h = 7 (SUCCESS) | T3-1 и T3-2 не пройдут | таблица сценариев `data/gateway_scenarios.csv` с приоритетом, затем формула ([ADR-001](../adr-001-gateway-emulator.md)) |
| `SECRET` зашит в код | противоречит 11_ИБ | `os.environ["TIPS_HMAC_SECRET"]`, без значения по умолчанию ([ADR-009](../adr-009-secrets-time-ttl.md)) |
| `check_exp()` — заглушка | все `exp` в 2025 году: честная проверка отклонит каждый QR | параметр `--now`, по умолчанию текущее время UTC |
| `calc_amounts()` во float | 53.50 × 5 % → 2.67 вместо 2.68 | `Decimal` + `ROUND_HALF_UP` ([ADR-004](../adr-004-validation-order.md)) |
| `payment_id = p_demo_{qr}`, каждый запуск дописывает строку | T3-4 не пройдёт | `idempotency_key` + последовательный `p_0101…` ([ADR-008](../adr-008-idempotency.md)) |
| Нет отмены, нет статусов CREATED/PENDING | T3-5 не пройдёт | подкоманды `create` / `confirm` / `cancel` ([ADR-002](../adr-002-payment-lifecycle.md)) |
| Нет аудита | раздел 9 шаблона 07 | `audit_out.csv` с 9 полями (A.2, раздел 2.6) |

## Что сделать

1. **Вход** — JSON: `{"qr_id", "sig", "base_amount", "tip_pct" | "tip_amount", "idempotency_key"}`.
   `sig` — подпись из ссылки (ADR-003): проверяй и её, и подпись записи в `qr_links.csv`.
2. **Порядок проверок** — строго по ADR-004: подпись → поиск по `idempotency_key` → exp → nonce →
   мерчант → чек → суммы. Ключ ищется до проверки nonce, иначе повтор после оплаты получит QR_USED.
   Повторные `confirm` и `cancel` отвечают по таблице в ADR-002.
3. **Сумма чека.** Реестр чеков для прототипа строится из `payments.csv`: `p_000N ↔ qr_00N`, `amount` = сумма чека.
   Несовпадение `base_amount` с реестром → `AMOUNT_MISMATCH`.
4. **Выход** — JSON с `payment_id`, `status`, `tip_amount`, `amount_total`, `error_code`.
   Коды выхода организаторов: 0 — SUCCESS, 1 — не SUCCESS, 2 — ошибка проверки.
5. **Запись** — `payments_out.csv`: 7 колонок seed в том же порядке плюс `qr_id`, `idempotency_key`,
   `amount_total`, `error_code` (ADR-005). `payments.csv` и остальные seed-CSV только читаются.
6. **Аудит** — `audit_out.csv`: событие на каждый переход статуса.

Seed-данные трогать нельзя. Для теста T3-7 нужен QR мерчанта m_019 (BLOCKED), которого нет в данных:
сгенерируй и подпиши его в отдельном `prototype/test_qr_links.csv` и подключай опцией `--qr-file`.

## Тесты и ожидаемый результат

Все запуски с `TIPS_HMAC_SECRET=demo-secret-k3` и `--now 2025-11-01T18:00:00Z`, если не сказано иное.

| Тест | Вход | Ожидание |
|-|-|-|
| T3-1 Успех | qr_001, 637.00, 5 % | SUCCESS, tip 31.85, total 668.85, код выхода 0 |
| T3-2 Отказ | qr_003, 911.00, 15 % | DECLINED, CARD_DECLINED, tip 136.65, total 1047.65 |
| T3-3 Неверный QR | qr_001, испорченный `sig` | INVALID_QR, код выхода 2, **строки в payments_out.csv нет** |
| T3-3б Истёк | qr_001, `--now 2025-11-02T00:00:00Z` | QR_EXPIRED |
| T3-4 Повтор | один `idempotency_key` дважды | тот же `payment_id`, в payments_out.csv **одна** строка |
| T3-5 Отмена | `create` → `cancel` | CANCELED; `cancel` для qr_010 в PENDING → CANCEL_NOT_ALLOWED |
| T3-6 Таймаут (доп.) | qr_010 | PENDING, GATEWAY_TIMEOUT |
| T3-7 Блокировка (доп.) | QR мерчанта m_019 | MERCHANT_BLOCKED |
| T3-8 Чаевые > 50 % (доп.) | qr_001, `tip_pct` 51 | TIP_OUT_OF_RANGE |
| T3-9 Повторная оплата (доп.) | qr_001 после T3-1, **новый** `idempotency_key` | QR_USED |
| T3-4б Повтор после оплаты (доп.) | qr_001 после T3-1, **тот же** `idempotency_key` | 200, тот же `payment_id`, не QR_USED |
| T3-5б Повторные действия (доп.) | повторный `confirm` после SUCCESS; `confirm` после `cancel`; повторный `cancel` | 200 без вызова шлюза; PAYMENT_STATE_CONFLICT; 200 CANCELED |

Перед каждым прогоном удаляй `payments_out.csv` и `audit_out.csv`, иначе T3-4 и T3-9 дадут ложный результат.
Хорошо бы обернуть всё в `prototype/run_tests.sh` или pytest, чтобы «Факт» в 03_тесты получался одной командой.

## Для приложения (раздел E)

Готовые картинки и демонстрация: `python3 prototype/demo.py`, скриншоты в [docs/screenshots](../screenshots/README.md)
(пересоздаются скриптом).

- [x] Скриншот вывода прототипа для qr_001 и qr_003 — `tests.png`, `demo.png`
- [x] Скриншот `cd prototype/01_данные && md5sum -c MD5SUMS` (все OK) — `md5.png`
- [ ] Скриншот последнего коммита в ветке `team_N_case_3`: `python3 docs/screenshots/make_screenshots.py commit` после создания ветки
- [x] «Факт» и «Пройден» в `docs/03-tests.md` по каждому тесту

## Готово, когда

- [ ] T3-1…T3-5 проходят, доп. тесты — по возможности
- [ ] Секрета в коде нет (`grep -r demo-secret prototype/` ничего не находит)
- [ ] Ты можешь объяснить каждую строку прототипа
- [ ] Итерации с ИИ записаны в `docs/ai-journal.md`
