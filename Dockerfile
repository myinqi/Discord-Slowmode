# Keep the runtime stable. The floating python:3.12-slim tag moved to Debian
# 13 / FFmpeg 7.x and correlated with Twitch playback drops after ~8 minutes.
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install ffmpeg for audio validation and Twitch streaming.
# libgomp1 is required by CTranslate2 (faster-whisper backend).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fontconfig libgomp1 tzdata \
        fonts-noto fonts-noto-mono fonts-noto-cjk fonts-noto-extra \
        fonts-freefont-otf \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f \
    && test -n "$(fc-match -f '%{file}' ':charset=2728')"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory
RUN mkdir -p /app/data

# Expose web interface port
EXPOSE 5000

# Ensure print() flushes immediately so `docker compose logs` shows output live.
ENV PYTHONUNBUFFERED=1

# Run the bot + web server
CMD ["python", "run.py"]
