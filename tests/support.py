"""Общие вещи интеграционных тестов с PostgreSQL."""
import os
import tempfile

_server = None


def database_url():
    """Адрес тестовой базы: TIPS_TEST_DATABASE_URL, иначе встроенный PostgreSQL (pgserver), иначе None."""
    global _server
    from tips.adapters.sql.engine import normalize_url

    url = os.environ.get("TIPS_TEST_DATABASE_URL")
    if url:
        return normalize_url(url)
    try:
        import pgserver
    except ImportError:
        return None
    if _server is None:
        _server = pgserver.get_server(tempfile.mkdtemp(prefix="tips_pg_"), cleanup_mode="stop")
    return normalize_url(_server.get_uri())
