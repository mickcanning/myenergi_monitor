FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=5000 \
    PUID=99 \
    PGID=100

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Uploaded logs, the current report and CT choices live here: map it to appdata to keep them.
VOLUME /app/instance
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5000'), timeout=4)"

# Starts as root only to fix ownership of the data folder, then runs as PUID:PGID (Unraid's nobody:users).
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
