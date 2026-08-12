from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import time
import logging
import os
import re
import zipfile
import tempfile
import subprocess
from requests.exceptions import ConnectionError
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ===== НАСТРОЙКА РОТАЦИИ КЛЮЧЕЙ =====
raw_keys = os.getenv('YOUTUBE_API_KEYS', '').split(',')
API_KEYS = []
for item in raw_keys:
    item = item.strip()
    if ':' in item:
        num, key = item.split(':', 1)
        API_KEYS.append((int(num.strip()), key.strip()))
    else:
        API_KEYS.append((len(API_KEYS)+1, item))

if not API_KEYS:
    logging.error("No API keys configured!")
    raise SystemExit("No API keys configured")

logging.info(f"Загружено {len(API_KEYS)} API ключей:")
for num, key in API_KEYS:
    logging.info(f"  Ключ {num}: {key[:10]}...")

current_index = 0
switch_count = 0

def get_current_key_info():
    return API_KEYS[current_index]

def switch_to_next_key():
    global current_index, switch_count
    if current_index < len(API_KEYS) - 1:
        current_index += 1
    else:
        current_index = 0
        switch_count += 1
    logging.info(f"Переключились на ключ {API_KEYS[current_index][0]}")
    return True

# ===== ЛОГИКА КОНВЕРТАЦИИ =====
def process_download(url):
    logging.info(f"Получен URL: {url}")
    cache_buster = int(time.time() * 1000)
    configs = [
        {"api": "convert1s", "bitrate": "320k", "timeout": 15, "attempts": 2, "delay": 2},
        {"api": "convert1s", "bitrate": "128k", "timeout": 15, "attempts": 3, "delay": 2}
    ]

    for config in configs:
        for attempt in range(config["attempts"]):
            try:
                if config["api"] == "convert1s":
                    headers = {
                        'accept': 'application/json',
                        'content-type': 'application/json',
                        'origin': 'https://media.ytmp3.gg',
                        'referer': 'https://media.ytmp3.gg/',
                        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                    fake_url = url + f"&_={cache_buster + attempt}"
                    payload = {
                        "url": fake_url,
                        "os": "windows",
                        "output": {"type": "audio", "format": "mp3"},
                        "audio": {"bitrate": config["bitrate"]}
                    }
                    resp = requests.post('https://hub.convert1s.com/api/download', json=payload, headers=headers, timeout=config["timeout"])
                    if resp.status_code != 200:
                        logging.warning(f"convert1s ({config['bitrate']}) попытка {attempt+1}: статус {resp.status_code}")
                        time.sleep(config["delay"])
                        continue
                    data = resp.json()
                    status_url = data.get('statusUrl')
                    if not status_url:
                        logging.warning(f"convert1s ({config['bitrate']}) попытка {attempt+1}: нет statusUrl")
                        time.sleep(config["delay"])
                        continue

                    for _ in range(8):
                        time.sleep(2)
                        try:
                            status_resp = requests.get(status_url, timeout=10)
                            if status_resp.status_code != 200:
                                continue
                            status_data = status_resp.json()
                            if 'downloadUrl' in status_data and status_data['downloadUrl']:
                                mp3_url = status_data['downloadUrl']
                                logging.info(f"Получена ссылка через convert1s ({config['bitrate']}) попытка {attempt+1}: {mp3_url}")
                                return {'link': mp3_url}
                            if status_data.get('status') == 'error' or status_data.get('state') == 'error':
                                break
                        except ConnectionError as e:
                            logging.error(f"convert1s ({config['bitrate']}) попытка {attempt+1}: ошибка соединения: {e}")
                            break
                        except Exception as e:
                            logging.error(f"convert1s ({config['bitrate']}) попытка {attempt+1}: ошибка при опросе: {e}")
                            continue
                    logging.warning(f"convert1s ({config['bitrate']}) попытка {attempt+1} не удалась")
                    time.sleep(config["delay"])
                    continue

            except Exception as e:
                logging.error(f"Ошибка в конфигурации {config['api']} попытка {attempt+1}: {str(e)}")
                time.sleep(config["delay"])
                continue

    return {'error': 'Conversion failed after all attempts'}

# ===== ЭНДПОИНТ /download =====
@app.route('/download', methods=['GET', 'OPTIONS'])
def download():
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    result = process_download(url)
    if 'error' in result:
        return jsonify(result), 500
    return jsonify(result)

# ===== ЭНДПОИНТ /playlist =====
@app.route('/playlist', methods=['GET'])
def playlist():
    global switch_count
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    logging.info(f"Received URL: {url}")

    match = re.search(r'[?&]list=([^&]+)', url)
    if not match:
        logging.error(f"No list parameter found in URL: {url}")
        return jsonify({'error': 'Invalid playlist URL: no list parameter found'}), 400
    playlist_id = match.group(1)
    logging.info(f"Extracted playlist ID: {playlist_id}")

    max_attempts = len(API_KEYS) * 3
    attempt = 0

    while attempt < max_attempts:
        num, current_key = get_current_key_info()
        logging.info(f"🔑 Ключ {num} - пытаемся использовать...")

        params = {
            'part': 'snippet',
            'maxResults': 50,
            'playlistId': playlist_id,
            'key': current_key
        }

        try:
            resp = requests.get('https://www.googleapis.com/youtube/v3/playlistItems', params=params, timeout=30)

            if resp.status_code == 200:
                logging.info(f"✅ Ключ {num} - работает (сейчас используется)")
                switch_count = 0
                data = resp.json()
                tracks = []
                playlist_title = 'YouTube Playlist'

                for item in data.get('items', []):
                    snippet = item.get('snippet', {})
                    video_id = snippet.get('resourceId', {}).get('videoId')
                    if video_id:
                        thumbnail = snippet.get('thumbnails', {}).get('default', {}).get('url', '')
                        if not thumbnail:
                            thumbnail = snippet.get('thumbnails', {}).get('medium', {}).get('url', '')
                        tracks.append({
                            'title': snippet.get('title', 'Unknown'),
                            'id': video_id,
                            'url': f"https://www.youtube.com/watch?v={video_id}",
                            'thumbnail': thumbnail
                        })
                        if snippet.get('playlistTitle') and playlist_title == 'YouTube Playlist':
                            playlist_title = snippet.get('playlistTitle')

                if not tracks:
                    return jsonify({'error': 'No tracks found'}), 404

                return jsonify({
                    'playlist': playlist_title,
                    'tracks': tracks,
                    'total': len(tracks)
                })

            elif resp.status_code == 403 and 'quotaExceeded' in resp.text:
                logging.warning(f"❌ Ключ {num} - не работает (квота исчерпана)")
                switch_to_next_key()
                attempt += 1
                if switch_count > 0 and switch_count % len(API_KEYS) == 0:
                    logging.warning("Все ключи перебраны без успеха. Пауза 60 секунд...")
                    time.sleep(60)
                continue

            else:
                logging.error(f"YouTube API error: {resp.status_code} - {resp.text}")
                return jsonify({'error': f'YouTube API error: {resp.status_code}'}), 500

        except Exception as e:
            logging.error(f"Playlist error: {str(e)}")
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'All API keys quota exceeded'}), 500

# ===== ФУНКЦИЯ ДЛЯ СКАЧИВАНИЯ MP3 (с ретраями) =====
def download_mp3(mp3_url, title, timeout=180):
    for attempt in range(3):
        try:
            logging.info(f"Скачивание {title} (попытка {attempt+1})")
            with requests.get(mp3_url, stream=True, timeout=(10, timeout)) as resp:
                if resp.status_code != 200:
                    logging.warning(f"Статус {resp.status_code} для {title}")
                    continue
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        chunks.append(chunk)
                        total += len(chunk)
                        if total % (1024*1024) < 8192:
                            logging.info(f"{title}: скачано {total // (1024*1024)} МБ")
                logging.info(f"{title}: успешно скачан, размер {total} байт")
                return b''.join(chunks)
        except Exception as e:
            logging.warning(f"Ошибка скачивания {title} (попытка {attempt+1}): {e}")
            time.sleep(5)
    return None

# ===== ЭНДПОИНТ /zip (параллельное получение ссылок + скачивание) =====
@app.route('/zip', methods=['POST'])
def zip_tracks():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    # 1. Параллельно получаем ссылки для всех треков
    mp3_urls = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_track = {
            executor.submit(process_download, track['url']): (idx, track['title'])
            for idx, track in enumerate(tracks)
        }
        for future in as_completed(future_to_track):
            idx, title = future_to_track[future]
            result = future.result()
            if 'link' in result:
                mp3_urls.append((idx, title, result['link']))
                logging.info(f"Ссылка получена для {title}")
            else:
                logging.warning(f"Не удалось получить ссылку для {title}: {result.get('error')}")

    if not mp3_urls:
        return jsonify({'error': 'No MP3 links obtained'}), 500

    # 2. Параллельное скачивание MP3
    downloaded = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_download = {
            executor.submit(download_mp3, url, title, 180): (idx, title)
            for idx, title, url in mp3_urls
        }
        for future in as_completed(future_to_download):
            idx, title = future_to_download[future]
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать {title}")
                continue
            downloaded.append((idx, title, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

    # 3. Упаковка в ZIP
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, 'playlist.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for idx, title, mp3_data in downloaded:
                    filename = f"{idx+1:02d} - {title}.mp3"
                    safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename)
                    filepath = os.path.join(tmpdir, safe_filename)
                    with open(filepath, 'wb') as f:
                        f.write(mp3_data)
                    zipf.write(filepath, safe_filename)
            return send_file(zip_path, as_attachment=True, download_name='playlist.zip')
    except Exception as e:
        logging.error(f"ZIP error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ЭНДПОИНТ /zip-from-links =====
@app.route('/zip-from-links', methods=['POST'])
def zip_from_links():
    data = request.get_json()
    if not data or 'links' not in data:
        return jsonify({'error': 'Missing links list'}), 400

    links = data['links']
    if not links:
        return jsonify({'error': 'Empty links list'}), 400

    downloaded = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_link = {executor.submit(download_mp3, link, f"track_{i+1}", 120): (i, f"track_{i+1}") for i, link in enumerate(links)}
        for future in as_completed(future_to_link):
            i, title = future_to_link[future]
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать {title}")
                continue
            downloaded.append((i, title, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, 'playlist.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for i, title, mp3_data in downloaded:
                    filename = f"track_{i+1:02d}.mp3"
                    filepath = os.path.join(tmpdir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(mp3_data)
                    zipf.write(filepath, filename)
            return send_file(zip_path, as_attachment=True, download_name='playlist.zip')
    except Exception as e:
        logging.error(f"ZIP from links error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ЭНДПОИНТ /merge =====
@app.route('/merge', methods=['POST'])
def merge_tracks():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    # 1. Параллельно получаем ссылки для всех треков
    mp3_urls = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_track = {
            executor.submit(process_download, track['url']): (idx, track['title'])
            for idx, track in enumerate(tracks)
        }
        for future in as_completed(future_to_track):
            idx, title = future_to_track[future]
            result = future.result()
            if 'link' in result:
                mp3_urls.append((idx, title, result['link']))
                logging.info(f"Ссылка получена для {title}")
            else:
                logging.warning(f"Не удалось получить ссылку для {title}: {result.get('error')}")

    if not mp3_urls:
        return jsonify({'error': 'No MP3 links obtained'}), 500

    # 2. Параллельное скачивание MP3
    downloaded = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_download = {
            executor.submit(download_mp3, url, title, 180): (idx, title)
            for idx, title, url in mp3_urls
        }
        for future in as_completed(future_to_download):
            idx, title = future_to_download[future]
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать {title}")
                continue
            downloaded.append((idx, title, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_files = []
            for idx, title, mp3_data in downloaded:
                filename = f"{idx:04d}.mp3"
                filepath = os.path.join(tmpdir, filename)
                with open(filepath, 'wb') as f:
                    f.write(mp3_data)
                mp3_files.append(filepath)

            list_path = os.path.join(tmpdir, 'list.txt')
            with open(list_path, 'w') as f:
                for mp3 in mp3_files:
                    f.write(f"file '{os.path.basename(mp3)}'\n")

            output_path = os.path.join(tmpdir, 'merged.mp3')
            cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output_path]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir, timeout=300)
            if result.returncode != 0:
                logging.error(f"FFmpeg error: {result.stderr}")
                return jsonify({'error': 'FFmpeg merge failed'}), 500

            return send_file(output_path, as_attachment=True, download_name='merged.mp3')
    except Exception as e:
        logging.error(f"Merge error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ЭНДПОИНТ /merge-from-links =====
@app.route('/merge-from-links', methods=['POST'])
def merge_from_links():
    data = request.get_json()
    if not data or 'links' not in data:
        return jsonify({'error': 'Missing links list'}), 400

    links = data['links']
    if not links:
        return jsonify({'error': 'Empty links list'}), 400

    downloaded = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_link = {executor.submit(download_mp3, link, f"track_{i+1}", 120): (i, f"track_{i+1}") for i, link in enumerate(links)}
        for future in as_completed(future_to_link):
            i, title = future_to_link[future]
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать {title}")
                continue
            downloaded.append((i, title, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_files = []
            for i, title, mp3_data in downloaded:
                filename = f"{i:04d}.mp3"
                filepath = os.path.join(tmpdir, filename)
                with open(filepath, 'wb') as f:
                    f.write(mp3_data)
                mp3_files.append(filepath)

            list_path = os.path.join(tmpdir, 'list.txt')
            with open(list_path, 'w') as f:
                for mp3 in mp3_files:
                    f.write(f"file '{os.path.basename(mp3)}'\n")

            output_path = os.path.join(tmpdir, 'merged.mp3')
            cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output_path]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir, timeout=300)
            if result.returncode != 0:
                logging.error(f"FFmpeg error: {result.stderr}")
                return jsonify({'error': 'FFmpeg merge failed'}), 500

            return send_file(output_path, as_attachment=True, download_name='merged.mp3')
    except Exception as e:
        logging.error(f"Merge from links error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/test-download', methods=['GET'])
def test_download():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    test_url = url

    # 1. Без заголовков
    try:
        resp1 = requests.get(test_url, timeout=10)
        result1 = {'status': resp1.status_code, 'len': len(resp1.content), 'preview': resp1.text[:200]}
    except Exception as e:
        result1 = {'error': str(e)}

    # 2. С браузерными заголовками
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'audio/mpeg,audio/*;q=0.9,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Referer': 'https://media.ytmp3.gg/',
        'Origin': 'https://media.ytmp3.gg/',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
    }
    try:
        resp2 = requests.get(test_url, headers=headers, timeout=10)
        result2 = {'status': resp2.status_code, 'len': len(resp2.content), 'preview': resp2.text[:200]}
    except Exception as e:
        result2 = {'error': str(e)}

    return jsonify({
        'without_headers': result1,
        'with_headers': result2
    })

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
