from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import time
import logging
import os

print("Starting app...")
print("Creating Flask app...")
app = Flask(__name__)
print("Flask app created")
CORS(app)
print("CORS enabled")
logging.basicConfig(level=logging.INFO)
print("Logging configured")

# ===== СУЩЕСТВУЮЩИЙ ЭНДПОИНТ /download =====
@app.route('/download', methods=['GET', 'OPTIONS'])
def download():
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    logging.info(f"Получен URL: {url}")

    # Делаем до 3 попыток
    for attempt in range(3):
        try:
            headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'origin': 'https://media.ytmp3.gg',
                'referer': 'https://media.ytmp3.gg/',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            payload = {
                "url": url,
                "os": "windows",
                "output": {"type": "audio", "format": "mp3"},
                "audio": {"bitrate": "320k"}
            }

            resp = requests.post('https://hub.convert1s.com/api/download', json=payload, headers=headers, timeout=30)
            if resp.status_code != 200:
                logging.warning(f"Попытка {attempt+1}: API вернул {resp.status_code}")
                continue

            data = resp.json()
            status_url = data.get('statusUrl')
            if not status_url:
                logging.warning(f"Попытка {attempt+1}: нет statusUrl")
                continue

            # Опрашиваем статус до 80 секунд (40 попыток * 2 сек)
            for _ in range(40):
                time.sleep(2)
                status_resp = requests.get(status_url, timeout=20)
                if status_resp.status_code != 200:
                    continue
                status_data = status_resp.json()
                if 'downloadUrl' in status_data and status_data['downloadUrl']:
                    mp3_url = status_data['downloadUrl']
                    logging.info(f"Получена ссылка (попытка {attempt+1}): {mp3_url}")
                    return jsonify({'link': mp3_url})
                if status_data.get('status') == 'error' or status_data.get('state') == 'error':
                    break

        except Exception as e:
            logging.error(f"Попытка {attempt+1} упала: {str(e)}")
            continue

        logging.warning(f"Попытка {attempt+1} не удалась, повторяем...")

    return jsonify({'error': 'Conversion timeout after multiple attempts'}), 500

# ===== НОВЫЙ ЭНДПОИНТ /playlist =====
@app.route('/playlist', methods=['GET'])
def playlist():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    # Извлекаем ID плейлиста из URL (более надёжный способ)
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    playlist_id = query_params.get('list', [None])[0]

    if not playlist_id:
        return jsonify({'error': 'Invalid playlist URL: no list parameter found'}), 400

    # Берём ключ из переменной окружения
    API_KEY = os.getenv('YOUTUBE_API_KEY')
    if not API_KEY:
        return jsonify({'error': 'YouTube API key not configured'}), 500

    try:
        # Запрашиваем список треков через YouTube API
        api_url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&maxResults=50&playlistId={playlist_id}&key={API_KEY}"
        resp = requests.get(api_url, timeout=30)
        if resp.status_code != 200:
            logging.error(f"YouTube API error: {resp.status_code} - {resp.text}")
            return jsonify({'error': f'YouTube API error: {resp.status_code}'}), 500

        data = resp.json()
        tracks = []
        for item in data.get('items', []):
            snippet = item.get('snippet', {})
            video_id = snippet.get('resourceId', {}).get('videoId')
            if video_id:
                tracks.append({
                    'title': snippet.get('title', 'Unknown'),
                    'id': video_id,
                    'url': f"https://www.youtube.com/watch?v={video_id}"
                })

        if not tracks:
            return jsonify({'error': 'No tracks found'}), 404

        # Название плейлиста
        playlist_title = data.get('items', [{}])[0].get('snippet', {}).get('playlistTitle', 'YouTube Playlist')

        return jsonify({
            'playlist': playlist_title,
            'tracks': tracks
        })

    except Exception as e:
        logging.error(f"Playlist error: {str(e)}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
