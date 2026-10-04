FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --system --uid 10001 --home /app finance \
    && mkdir -p /data && chown finance:finance /data
COPY --chown=finance:finance alembic.ini ./
COPY --chown=finance:finance migrations ./migrations
COPY --chown=finance:finance app ./app
COPY --chown=finance:finance docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

USER finance
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
