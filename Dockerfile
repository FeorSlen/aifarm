FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 \
        libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 \
        libgtk-3-0 libnspr4 libnss3 libx11-xcb1 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libxshmfence1 libxtst6 xauth \
        x11vnc xdg-utils xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "camoufox[geoip]"

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/profiles /data/uploads \
    && chown app:app /data/profiles /data/uploads
ENV HOME=/home/app
USER app
RUN python -m camoufox fetch

USER root
RUN mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix
COPY --chown=app:app . .
USER app
