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
import io
from requests.exceptions import ConnectionError
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ===== ПРОКСИ (если задан в переменной окружения) =====
PROXY = os.getenv('PROXY', None)
if PROXY:
    logging.info(f"Будет использован прокси: {PROXY[:20]}...")

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

# ===== ОБЩИЕ ЗАГОЛОВКИ =====
COMMON_HEADERS = {
    'origin': 'https://media.ytmp3.gg',
    'referer': 'https://media.ytmp3.gg/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
}

# ===== ЛОГИКА КОНВЕРТАЦИИ (с сессией) =====
def process_download(url):
    logging.info(f"Получен URL: {url}")
    cache_buster = int(time.time() * 1000)
    configs = [
        {"api": "convert1s", "bitrate": "320k", "timeout": 15, "attempts": 2, "delay": 2},
        {"api": "convert1s", "bitrate": "128k", "timeout": 15, "attempts": 3, "delay": 2}
    ]

    for config in configs:
        for attempt in range(config["attempts"]):
            session = requests.Session()
            session.headers.update(COMMON_HEADERS)
            if PROXY:
                session.proxies = {'http': PROXY, 'https': PROXY}
            try:
                if config["api"] == "convert1s":
                    post_headers = {
                        **COMMON_HEADERS,
                        'accept': 'application/json',
                        'content-type': 'application/json',
                    }
                    fake_url = url + f"&_={cache_buster + attempt}"
                    payload = {
                        "url": fake_url,
                        "os": "windows",
                        "output": {"type": "audio", "format": "mp3"},
                        "audio": {"bitrate": config["bitrate"]}
                    }
                    resp = session.post('https://hub.convert1s.com/api/download', json=payload, headers=post_headers, timeout=config["timeout"])
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
                            status_resp = session.get(status_url, timeout=10)
                            if status_resp.status_code != 200:
                                continue
                            status_data = status_resp.json()
                            if 'downloadUrl' in status_data and status_data['downloadUrl']:
                                mp3_url = status_data['downloadUrl']
                                logging.info(f"Получена ссылка через convert1s ({config['bitrate']}) попытка {attempt+1}: {mp3_url}")
                                return {'link': mp3_url, 'session': session}
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
    return jsonify({'link': result['link']})

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

# ===== ФУНКЦИЯ ДЛЯ СКАЧИВАНИЯ MP3 (с прокси и сессией) =====
DOWNLOAD_HEADERS = {
    'Accept': '*/*',
    'Accept-Encoding': 'identity',
    'Connection': 'keep-alive',
    **COMMON_HEADERS,
}

def download_mp3(mp3_url, title, timeout=180, session=None):
    client = session if session is not None else requests
    proxies = {'http': PROXY, 'https': PROXY} if PROXY else None
    for attempt in range(3):
        try:
            logging.info(f"Скачивание {title} (попытка {attempt+1})" + (f" через прокси" if PROXY else ""))
            with client.get(mp3_url, headers=DOWNLOAD_HEADERS, proxies=proxies, stream=True, timeout=(10, timeout)) as resp:
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

# ===== ЭНДПОИНТ /zip (пониженный параллелизм) =====
@app.route('/zip', methods=['POST'])
def zip_tracks():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    mp3_urls = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_track = {
            executor.submit(process_download, track['url']): (idx, track['title'])
            for idx, track in enumerate(tracks)
        }
        for future in as_completed(future_to_track):
            idx, title = future_to_track[future]
            result = future.result()
            if 'link' in result:
                mp3_urls.append((idx, title, result['link'], result.get('session')))
                logging.info(f"Ссылка получена для {title}")
            else:
                logging.warning(f"Не удалось получить ссылку для {title}: {result.get('error')}")

    if not mp3_urls:
        return jsonify({'error': 'No MP3 links obtained'}), 500

    downloaded = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for idx, title, url, sess in mp3_urls:
            time.sleep(0.5)
            futures.append((idx, title, executor.submit(download_mp3, url, title, 180, sess)))
        for idx, title, future in futures:
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать {title}")
                continue
            downloaded.append((idx, title, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

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
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for i, link in enumerate(links):
            time.sleep(0.5)
            futures.append((i, executor.submit(download_mp3, link, f"track_{i+1}", 120)))
        for i, future in futures:
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать track_{i+1}")
                continue
            downloaded.append((i, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, 'playlist.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for i, mp3_data in downloaded:
                    filename = f"track_{i+1:02d}.mp3"
                    filepath = os.path.join(tmpdir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(mp3_data)
                    zipf.write(filepath, filename)
            return send_file(zip_path, as_attachment=True, download_name='playlist.zip')
    except Exception as e:
        logging.error(f"ZIP from links error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ЭНДПОИНТ /merge (пониженный параллелизм) =====
@app.route('/merge', methods=['POST'])
def merge_tracks():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    mp3_urls = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_track = {
            executor.submit(process_download, track['url']): (idx, track['title'])
            for idx, track in enumerate(tracks)
        }
        for future in as_completed(future_to_track):
            idx, title = future_to_track[future]
            result = future.result()
            if 'link' in result:
                mp3_urls.append((idx, title, result['link'], result.get('session')))
                logging.info(f"Ссылка получена для {title}")
            else:
                logging.warning(f"Не удалось получить ссылку для {title}: {result.get('error')}")

    if not mp3_urls:
        return jsonify({'error': 'No MP3 links obtained'}), 500

    downloaded = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for idx, title, url, sess in mp3_urls:
            time.sleep(0.5)
            futures.append((idx, title, executor.submit(download_mp3, url, title, 180, sess)))
        for idx, title, future in futures:
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
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for i, link in enumerate(links):
            time.sleep(0.5)
            futures.append((i, executor.submit(download_mp3, link, f"track_{i+1}", 120)))
        for i, future in futures:
            data = future.result()
            if data is None:
                logging.warning(f"Не удалось скачать track_{i+1}")
                continue
            downloaded.append((i, data))

    if not downloaded:
        return jsonify({'error': 'No MP3 files downloaded'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_files = []
            for i, mp3_data in downloaded:
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

# ===== ТЕСТОВЫЙ ЭНДПОИНТ (для диагностики) =====
@app.route('/test-download', methods=['GET'])
def test_download():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    test_url = url

    try:
        resp1 = requests.get(test_url, timeout=10)
        result1 = {'status': resp1.status_code, 'len': len(resp1.content), 'preview': resp1.text[:200]}
    except Exception as e:
        result1 = {'error': str(e)}

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

# ===== ЭНДПОИНТ /links (только получить ссылки, без скачивания файлов) =====
# Использует браузер пользователя для самого скачивания — сервер отдаёт только
# прямые mp3-ссылки, полученные через process_download().
@app.route('/links', methods=['POST'])
def get_links():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    results = [None] * len(tracks)
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_idx = {
            executor.submit(process_download, track['url']): idx
            for idx, track in enumerate(tracks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            result = future.result()
            title = tracks[idx].get('title', f'track_{idx+1}')
            if 'link' in result:
                results[idx] = {'idx': idx, 'title': title, 'url': result['link'], 'ok': True}
                logging.info(f"Ссылка получена для {title}")
            else:
                results[idx] = {'idx': idx, 'title': title, 'ok': False, 'error': result.get('error')}
                logging.warning(f"Не удалось получить ссылку для {title}: {result.get('error')}")

    ok_count = sum(1 for r in results if r['ok'])
    if ok_count == 0:
        return jsonify({'error': 'No MP3 links obtained'}), 500

    return jsonify({'tracks': results, 'total': len(tracks), 'ok': ok_count})

# ===== ЭНДПОИНТ /zip-upload (браузер скачал mp3 сам и грузит нам байты для упаковки) =====
@app.route('/zip-upload', methods=['POST'])
def zip_upload():
    # Ожидается multipart/form-data:
    #   files   — несколько файлов (в нужном порядке добавления)
    #   titles  — JSON-массив названий в том же порядке (опционально)
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    titles = request.form.getlist('titles')

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, 'playlist.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for idx, f in enumerate(files):
                    title = titles[idx] if idx < len(titles) else f"track_{idx+1}"
                    filename = f"{idx+1:02d} - {title}.mp3"
                    safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename)
                    filepath = os.path.join(tmpdir, safe_filename)
                    f.save(filepath)
                    zipf.write(filepath, safe_filename)
            return send_file(zip_path, as_attachment=True, download_name='playlist.zip')
    except Exception as e:
        logging.error(f"ZIP upload error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ЭНДПОИНТ /merge-upload (браузер скачал mp3 сам и грузит нам байты для склейки) =====
@app.route('/merge-upload', methods=['POST'])
def merge_upload():
    # Ожидается multipart/form-data:
    #   files — несколько mp3-файлов В ПРАВИЛЬНОМ ПОРЯДКЕ склейки
    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_files = []
            for idx, f in enumerate(files):
                filename = f"{idx:04d}.mp3"
                filepath = os.path.join(tmpdir, filename)
                f.save(filepath)
                mp3_files.append(filepath)

            list_path = os.path.join(tmpdir, 'list.txt')
            with open(list_path, 'w') as fh:
                for mp3 in mp3_files:
                    fh.write(f"file '{os.path.basename(mp3)}'\n")

            output_path = os.path.join(tmpdir, 'merged.mp3')
            cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output_path]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir, timeout=300)
            if result.returncode != 0:
                logging.error(f"FFmpeg error: {result.stderr}")
                return jsonify({'error': 'FFmpeg merge failed'}), 500

            return send_file(output_path, as_attachment=True, download_name='merged.mp3')
    except Exception as e:
        logging.error(f"Merge upload error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ПРОКСИ-ЭНДПОИНТ ДЛЯ СКАЧИВАНИЯ MP3 НА СЕРВЕРЕ =====
@app.route('/proxy-mp3', methods=['GET'])
def proxy_mp3():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    logging.info(f"Proxy request for: {url[:80]}...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://media.ytmp3.gg/',
        'Origin': 'https://media.ytmp3.gg/'
    }

    for attempt in range(3):  # до 3 попыток
        try:
            logging.info(f"Proxy attempt {attempt+1} for {url[:80]}")
            resp = requests.get(url, headers=headers, stream=True, timeout=(10, 120))
            if resp.status_code != 200:
                logging.warning(f"Proxy status {resp.status_code} for {url[:80]}")
                if attempt == 2:
                    return jsonify({'error': f'Status {resp.status_code}'}), resp.status_code
                time.sleep(2)
                continue

            return send_file(
                io.BytesIO(resp.content),
                mimetype='audio/mpeg',
                as_attachment=True,
                download_name='track.mp3'
            )
        except Exception as e:
            logging.error(f"Proxy error attempt {attempt+1}: {str(e)}")
            if attempt == 2:
                return jsonify({'error': str(e)}), 500
            time.sleep(2)

    return jsonify({'error': 'Proxy failed after retries'}), 500

@app.route('/proxy-mp3-session', methods=['POST'])
def proxy_mp3_session():
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({'error': 'Missing url'}), 400

    url = data['url']
    cookies = data.get('cookies', {})

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://media.ytmp3.gg/',
        'Origin': 'https://media.ytmp3.gg/'
    }

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=(10, 120))
            if resp.status_code != 200:
                if attempt == 2:
                    return jsonify({'error': f'Status {resp.status_code}'}), resp.status_code
                time.sleep(2)
                continue
            return send_file(
                io.BytesIO(resp.content),
                mimetype='audio/mpeg',
                as_attachment=True,
                download_name='track.mp3'
            )
        except Exception as e:
            if attempt == 2:
                return jsonify({'error': str(e)}), 500
            time.sleep(2)

    return jsonify({'error': 'Proxy failed'}), 500

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
