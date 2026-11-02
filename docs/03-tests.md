# Тесты — кейс 3 (заполненный шаблон 03_тесты)

Прогон: `export TIPS_HMAC_SECRET=<демо-ключ из README> && python3 prototype/run_tests.py`.
Все запуски с `--now 2025-11-01T18:00:00Z`, если не сказано иное. Результат: 13 из 13.

| Тест | Вход | Ожидание | Факт | Пройден |
|-|-|-|-|-|
| T3-1 Успех | qr_001, 637.00, 5 % | SUCCESS, tip 31.85, total 668.85, выход 0 | выход 0, SUCCESS, tip 31.85, total 668.85 | да |
| T3-2 Отказ | qr_003, 911.00, 15 % | DECLINED, CARD_DECLINED, tip 136.65, total 1047.65 | выход 1, DECLINED, CARD_DECLINED, tip 136.65, total 1047.65 | да |
| T3-3 Неверный QR | qr_001, испорченный `sig` | INVALID_QR, выход 2, строки нет | выход 2, INVALID_QR, строк в payments_out.csv: 0 | да |
| T3-3б Истёк | qr_001, `--now 2025-11-02T00:00:00Z` | QR_EXPIRED | выход 2, QR_EXPIRED, строк: 0 | да |
| T3-3в Чужой чек | qr_004, `receipt_id` r_9999 | RECEIPT_MISMATCH, выход 2, строки нет | выход 2, RECEIPT_MISMATCH, строк: 0 | да |
| T3-4 Повтор | один `idempotency_key` дважды | тот же `payment_id`, одна строка | p_0101 и p_0101, строк: 1 | да |
| T3-4б Повтор после оплаты | qr_001 после T3-1, тот же `idempotency_key` | 200, тот же `payment_id`, не QR_USED | выход 0, SUCCESS, p_0101, строк: 1 | да |
| T3-5 Отмена | `create` → `cancel`; qr_010 в PENDING → `cancel` | CANCELED; CANCEL_NOT_ALLOWED | CREATED → CANCELED; из PENDING: CANCEL_NOT_ALLOWED | да |
| T3-5б Повторные действия | повторный `confirm` после SUCCESS; `confirm` после `cancel`; повторный `cancel` | 200 без вызова шлюза; PAYMENT_STATE_CONFLICT; 200 CANCELED | confirm после SUCCESS: SUCCESS, новых событий аудита нет; confirm после cancel: PAYMENT_STATE_CONFLICT; повторный cancel: CANCELED | да |
| T3-6 Таймаут | qr_010 | PENDING, GATEWAY_TIMEOUT | выход 1, PENDING, GATEWAY_TIMEOUT | да |
| T3-7 Блокировка | QR m_019 (`test_qr_links.csv`) | MERCHANT_BLOCKED | выход 2, MERCHANT_BLOCKED, строк: 0 | да |
| T3-8 Чаевые > 50 % | qr_001, `tip_pct` 51 | TIP_OUT_OF_RANGE | выход 2, TIP_OUT_OF_RANGE | да |
| T3-9 Повторная оплата | qr_001 после T3-1, другой ключ | QR_USED | выход 2, QR_USED, строк: 1 | да |

## Ограничения прототипа

- Статус платежа и событие аудита пишутся двумя отдельными записями (`payments_out.csv`, затем
  `audit_out.csv`). При обрыве между ними статус сменится без события. В реальной системе обе записи
  идут в одной транзакции БД (ADR-004, шаг 8).
