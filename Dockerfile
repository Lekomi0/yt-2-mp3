FROM python:3.11-slim

# ffmpeg нужен для конвертации в mp3 и склейки треков
# curl/unzip нужны для установки Deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno — JS-движок, который yt-dlp использует для решения подписи (n-challenge)
# у web-клиента YouTube. Без него web-клиент отдаёт только превью-картинки.
ENV DENO_INSTALL="/usr/local"
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
