FROM python:3.12-slim

WORKDIR /app

# Install ffmpeg for audio validation and Twitch streaming
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-noto fonts-noto-mono && rm -rf /var/lib/apt/lists/* && fc-cache -f

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
