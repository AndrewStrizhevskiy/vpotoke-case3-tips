"""Выпуск ключей для сервиса.

    python -m tips.http.keys [kid]       ключ подписи JWT (ES256): переменные TIPS_JWT_*
    python -m tips.http.keys qr [kid]    ключ подписи QR версии 2 (HMAC, 256 бит): TIPS_QR_KEYS и TIPS_QR_ACTIVE_KID

Печатается только в стандартный вывод; ключ нигде не сохраняется. Ключи хранят в менеджере секретов, а не в репозитории.
"""
import base64
import secrets
import sys
from datetime import datetime, timezone

from tips.adapters.jwt_keys import generate_key_pem


def jwt_key(kid):
    private, public = generate_key_pem()
    print(f"export TIPS_JWT_KID={kid}")
    print("export TIPS_JWT_PRIVATE_KEY=\"" + private.strip().replace("\n", "\\n") + "\"")
    print(f"# открытый ключ {kid} (при ротации добавьте его в TIPS_JWT_RETIRED_PUBLIC_KEYS на время жизни токенов):")
    print("# " + public.strip().replace("\n", "\n# "))


def qr_key(kid):
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    print(f"export TIPS_QR_ACTIVE_KID={kid}")
    print(f"export TIPS_QR_KEYS=\"{kid}:{key}\"")
    print("# при ротации оставьте старый ключ в списке через запятую (\"k1:...,k2:...\") на время жизни QR (4 часа),")
    print("# а активным сделайте новый: старые QR проверяются, новые подписываются новым ключом.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "qr":
        qr_key(argv[1] if len(argv) > 1 else datetime.now(timezone.utc).strftime("q%m%d%H"))
    else:
        jwt_key(argv[0] if argv else datetime.now(timezone.utc).strftime("k%Y%m%d"))


if __name__ == "__main__":
    main()
