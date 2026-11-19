FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=5001 \
    PUID=99 \
    PGID=100

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# The app runs as PUID (not root): make sure it can read the code whatever the source files' permissions were
# (e.g. a Mac checkout with 600 files), while leaving it read-only to the app.
RUN chmod -R a+rX,go-w /app

# Uploaded logs, the current report and CT choices live here: map it to appdata to keep them.
VOLUME /app/instance
EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5001'), timeout=4)"

# Starts as root only to fix ownership of the data folder, then runs as PUID:PGID (Unraid's nobody:users).
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
