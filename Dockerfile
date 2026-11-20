# Paper-trading desk. No broker order code exists in this image.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts
RUN pip install -r requirements.txt && pip install . && useradd -m desk && mkdir -p /app/data && chown -R desk /app/data
USER desk
ENV TRADEDESK_DB_PATH=/app/data/tradedesk.db TRADEDESK_API_HOST=0.0.0.0 TRADEDESK_API_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "uvicorn", "--factory", "tradedesk.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
