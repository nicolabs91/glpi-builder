FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_PORT=8080
ENV BASE_PATH=/volume1/docker
ENV BACKUP_ROOT=/volume1/docker/_BACKUPS
ENV TZ=Europe/Brussels

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY docker-compose.app.yml .
COPY backup ./backup
COPY tests ./tests

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["python", "app.py"]
