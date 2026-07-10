FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_PORT=8080
ENV BASE_PATH=/volume1/docker
ENV BACKUP_ROOT=/volume1/docker/_BACKUPS
ENV TZ=Europe/Amsterdam

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080

CMD ["python", "app.py"]
