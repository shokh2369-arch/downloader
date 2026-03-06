# Telegram media downloader bot: YouTube (youtube-dl), Instagram, Facebook, TikTok, X, Pinterest (yt-dlp)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HEALTH_PORT=8080 \
    PIP_ROOT_USER_ACTION=ignore

# ffmpeg (video re-encode), curl (healthcheck), deps for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium (Facebook/Twitter/Pinterest/cookies, headless)
RUN playwright install chromium

COPY bot.py .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/health || exit 1

CMD ["python", "bot.py"]
