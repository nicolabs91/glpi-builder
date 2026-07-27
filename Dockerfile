FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_PORT=8080
ENV BASE_PATH=/volume1/docker
ENV BACKUP_ROOT=/volume1/docker/_BACKUPS
ENV TZ=Europe/Brussels

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash ca-certificates coreutils curl findutils gzip tar \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY app_ui.py .
COPY auth_security.py .
COPY docker-compose.app.yml .
COPY install_on_synology.sh .
COPY rollback_on_synology.sh .
COPY scripts ./scripts
COPY backup ./backup
COPY tests ./tests

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["sh", "-c", "python -c 'import app; app.install_backup_dispatcher()' && exec gunicorn --bind=0.0.0.0:8080 --workers=1 --threads=4 --timeout=600 --access-logfile=- --error-logfile=- app:app"]
