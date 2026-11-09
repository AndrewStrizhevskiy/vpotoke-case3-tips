"""Выпуск нового ключа подписи JWT: python -m tips.http.keys [kid]. Печатает переменные окружения для сервиса."""
import sys
from datetime import datetime, timezone

from tips.adapters.jwt_keys import generate_key_pem


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    kid = argv[0] if argv else datetime.now(timezone.utc).strftime("k%Y%m%d")
    private, public = generate_key_pem()
    print(f"export TIPS_JWT_KID={kid}")
    print("export TIPS_JWT_PRIVATE_KEY=\"" + private.strip().replace("\n", "\\n") + "\"")
    print(f"# открытый ключ {kid} (при ротации добавьте его в TIPS_JWT_RETIRED_PUBLIC_KEYS на время жизни токенов):")
    print("# " + public.strip().replace("\n", "\n# "))


if __name__ == "__main__":
    main()
