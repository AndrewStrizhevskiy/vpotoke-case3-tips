#!/bin/sh
# Восстановление копии в ПУСТУЮ базу: ./deploy/restore.sh файл.dump
# Целевая база берётся из TIPS_DATABASE_URL; непустую базу скрипт не трогает. Затем проверка: deploy/verify.sh
set -eu
FILE="${1:?укажите файл копии}"
URL="$(printf '%s' "${TIPS_DATABASE_URL:?не задан TIPS_DATABASE_URL}" | sed 's/^postgresql+psycopg:/postgresql:/')"
BIN="${PG_BIN:+$PG_BIN/}"
TABLES="$("${BIN}psql" "$URL" -Atc "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema')")"
if [ "$TABLES" != "0" ]; then
  echo "Ошибка: целевая база не пуста ($TABLES таблиц); восстановление отменено" >&2
  exit 1
fi
"${BIN}pg_restore" --no-owner --exit-on-error --dbname="$URL" "$FILE"
echo "восстановлено из $FILE"
