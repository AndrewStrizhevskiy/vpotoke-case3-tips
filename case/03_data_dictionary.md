\# Data dictionary — кейс 3



\## Как заполнять

Четыре файла: merchants (4 колонки), employees (4), payments (7), qr\_links (7).

Два образца — по merchants и payments. Остальные — заполни сам.



\## Образец merchants (не трогать)



| Файл | Поле | Тип | Обяз. | Ограничение | Пример | Код ошибки |

|---|---|---|---|---|---|---|

| merchants | merchant\_id | string | да | m\_NNN | m\_001 | — |

| merchants | name | string | да | 1–100 символов | Вкус-Сервис Тверская | — |

| merchants | status | enum | да | ACTIVE/BLOCKED/PENDING | ACTIVE | MERCHANT\_BLOCKED |

| merchants | description | string | нет | 0–200 | Ресторан сети | — |



\## Образец payments (не трогать)



| Файл | Поле | Тип | Обяз. | Ограничение | Пример | Код ошибки |

|---|---|---|---|---|---|---|

| payments | payment\_id | string | да | p\_NNNN | p\_0001 | — |

| payments | amount | decimal | да | > 0, ≤ 1 000 000 | 637.00 | AMOUNT\_OUT\_OF\_RANGE |

| payments | tip | decimal | да | ≥ 0, ≤ amount × 0.5 | 63.70 | TIP\_OUT\_OF\_RANGE |

| payments | status | enum | да | CREATED/PENDING/SUCCESS/DECLINED/CANCELED | SUCCESS | — |



\## Твоя таблица — employees



| Поле | Тип | Обяз. | Ограничение | Пример | Код ошибки |

|---|---|---|---|---|---|

| employee\_id | ? | ? | ? | ? | ? |

| merchant\_id | ? | ? | ? | ? | ? |

| name | ? | ? | ? | ? | ? |

| payment\_details | ? | ? | ? | ? | ? |



\## Твоя таблица — qr\_links



| Поле | Тип | Обяз. | Ограничение | Пример | Код ошибки |

|---|---|---|---|---|---|

| qr\_id | ? | ? | ? | ? | ? |

| merchant\_id | ? | ? | ? | ? | ? |

| employee\_id | ? | ? | ? | ? | ? |

| receipt\_id | ? | ? | ? | ? | ? |

| nonce | ? | ? | ? | ? | ? |

| exp | ? | ? | ? | ? | ? |

| signature | ? | ? | ? | ? | ? |



\## Критерий приёмки

\- \[ ] Все 4 файла описаны (22 колонки)

\- \[ ] Для каждого поля указан тип и ограничение

\- \[ ] Для полей с валидацией — код ошибки из 08\_errors.md

\- \[ ] Пример взят из реального CSV

\- \[ ] payment\_details — только маска (\*\*\*\*NNNN)



\## Подсказки

\- payment\_details — это реквизиты для выплат, не для оплаты.

\- nonce — одноразовый, 6 hex.

\- exp — время, после которого QR недействителен (обычно 24 ч).

\- signature — HMAC-SHA256, 16 hex.

