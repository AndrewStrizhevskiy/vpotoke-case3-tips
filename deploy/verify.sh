#!/bin/sh
# Проверка восстановленной базы: версия миграций, число платежей и журнала, целостность аудита.
# ./deploy/verify.sh  (TIPS_DATABASE_URL указывает на проверяемую базу). Код выхода не 0, если что-то не сходится.
set -eu
URL="$(printf '%s' "${TIPS_DATABASE_URL:?не задан TIPS_DATABASE_URL}" | sed 's/^postgresql+psycopg:/postgresql:/')"
BIN="${PG_BIN:+$PG_BIN/}"
Q() { "${BIN}psql" "$URL" -Atc "$1"; }
echo "версия миграций: $(Q 'select version_num from alembic_version')"
echo "платежей: $(Q 'select count(*) from payments')"
echo "записей аудита: $(Q 'select count(*) from audit_events')"
ORPHANS="$(Q 'select count(*) from payments p where not exists (select 1 from audit_events a where a.payment_id = p.payment_id)')"
echo "платежей без аудита: $ORPHANS"
[ "$ORPHANS" = "0" ] || { echo "Ошибка: у платежей нет записей аудита" >&2; exit 1; }
