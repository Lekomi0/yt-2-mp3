FROM python:3.11-slim

# ffmpeg нужен только для склейки треков в /merge (notube сам отдаёт
# готовые mp3, конвертация нам больше не нужна)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
