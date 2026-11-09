#!/bin/sh
# Резервная копия базы: ./deploy/backup.sh [каталог] [сколько дней хранить]
# Нужны TIPS_DATABASE_URL (формат postgresql://...; префикс +psycopg отбрасывается) и pg_dump (или PG_BIN=/путь/к/bin).
# Копия в формате custom (pg_restore), права 600; старше N дней удаляются. Для восстановления на момент времени
# дополнительно включите архивацию WAL на сервере базы (см. docs/product/operations.md).
set -eu
DIR="${1:-./backups}"
KEEP="${2:-14}"
URL="$(printf '%s' "${TIPS_DATABASE_URL:?не задан TIPS_DATABASE_URL}" | sed 's/^postgresql+psycopg:/postgresql:/')"
BIN="${PG_BIN:+$PG_BIN/}"
mkdir -p "$DIR"
umask 077
FILE="$DIR/tips-$(date -u +%Y%m%dT%H%M%SZ).dump"
"${BIN}pg_dump" --format=custom --no-owner --file="$FILE.part" "$URL"
mv "$FILE.part" "$FILE"
find "$DIR" -name 'tips-*.dump' -mtime "+$KEEP" -delete
echo "$FILE"
