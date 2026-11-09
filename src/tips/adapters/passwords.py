"""Пароли: argon2id. В базе только хеш; параметры хранятся внутри хеша, поэтому их можно ужесточать без миграции.

Хеширование дорогое (десятки миллисекунд и десятки мегабайт), поэтому одновременных операций не больше max_parallel:
иначе поток запросов на /auth/login с несуществующими логинами исчерпал бы память и процессор. Лишние запросы
получают RateLimited (HTTP 429), а не очередь. Лимиты по адресу клиента добавляются отдельно (M5).
"""
import threading
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from tips.domain.errors import RateLimited

MIN_LENGTH = 12
MAX_LENGTH = 128  # длинный пароль не должен давать возможность нагрузить сервер хешированием
WAIT_SECONDS = 1.0


def normalize(password):
    """NFKC: один и тот же пароль, набранный на разных клавиатурах, не должен давать разные хеши."""
    return unicodedata.normalize("NFKC", password)


class Passwords:
    def __init__(self, hasher=None, max_parallel=4):
        self._hasher = hasher or PasswordHasher()  # argon2id с рекомендуемыми параметрами библиотеки
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._dummy = self._hasher.hash("недействительный пароль для выравнивания времени")  # считается заранее

    def _guard(self):
        if not self._slots.acquire(timeout=WAIT_SECONDS):
            raise RateLimited()
        return self._slots

    def hash(self, password):
        slots = self._guard()
        try:
            return self._hasher.hash(normalize(password))
        finally:
            slots.release()

    def verify(self, password_hash, password):
        slots = self._guard()
        try:
            return self._hasher.verify(password_hash, normalize(password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        finally:
            slots.release()

    def needs_rehash(self, password_hash):
        return self._hasher.check_needs_rehash(password_hash)

    def burn(self, password):
        """Пустая проверка вместо настоящей (нет такого логина или учётная запись закрыта): время ответа то же."""
        self.verify(self._dummy, password)
