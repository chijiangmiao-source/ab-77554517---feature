FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    APP_DB=/data/locator.db

WORKDIR /app

# 应用仅使用 Python 标准库，无第三方依赖需要安装。
COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

RUN mkdir -p /data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('APP_PORT','8080')+'/healthz', timeout=3).status==200 else 1)"

CMD ["python", "app/server.py"]
