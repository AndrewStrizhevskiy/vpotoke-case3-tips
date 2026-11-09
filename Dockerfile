# Образ сервиса чаевых. Сборка: docker build -t tips .
# Зависимости ставятся из pyproject (extras sql и http); исходники лежат в /app, потому что сервис ищет
# migrations/, alembic.ini и web/ рядом с пакетом (REPO_ROOT), поэтому пакет не устанавливается в site-packages.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/src
WORKDIR /app

COPY pyproject.toml ./
RUN python -c "import tomllib; e = tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']; \
print('\\n'.join(e['sql'] + e['http']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY web ./web
COPY data ./data

RUN useradd --system --uid 10001 --no-create-home tips
USER tips
EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8083/health', timeout=2).status == 200 else 1)"

# сервис; воркер сверки запускается тем же образом: python -m tips.jobs.worker; миграции: python -m tips.adapters.sql.admin migrate
CMD ["python", "-m", "tips.http"]
