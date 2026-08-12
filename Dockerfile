FROM python:3.11-slim

# Устанавливаем системные зависимости и yt-dlp
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем yt-dlp через pip
RUN pip install --no-cache-dir yt-dlp

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt

CMD python app.py
