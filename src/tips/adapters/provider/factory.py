"""Выбор платёжного провайдера по TIPS_PROVIDER. Адаптеры реальных провайдеров подключаются здесь."""
import os
import sys

from tips.adapters.provider.sandbox import SandboxProvider


def make_provider(production):
    """sandbox по умолчанию вне production; в production песочница запрещена, и без реального провайдера платежи
    API v2 не подтверждаются (выпуск QR и просмотр формы работают)."""
    name = os.environ.get("TIPS_PROVIDER", "" if production else "sandbox")
    if name == "sandbox":
        if production:
            sys.exit("Ошибка: песочный провайдер запрещён в production (TIPS_ENV не development)")
        return SandboxProvider()
    if not name:
        return None
    sys.exit(f"Ошибка: неизвестный провайдер TIPS_PROVIDER={name}")
