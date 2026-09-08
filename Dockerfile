# Same build shape as the MZ-Downloader project, which deploys reliably on
# Railway: python:3.12-slim + ffmpeg (for yt-dlp merge) + pinned deps.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg is required by yt-dlp (merging video+audio / HLS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY main.py .env.example README.md ./

CMD ["python", "-m", "bot.main"]
