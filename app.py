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
import uuid
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ===== НАСТРОЙКА РОТАЦИИ КЛЮЧЕЙ YOUTUBE DATA API =====
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

def youtube_api_get(endpoint, params, max_attempts=None):
    """Общая обёртка над YouTube Data API с ротацией ключей."""
    global switch_count
    if max_attempts is None:
        max_attempts = len(API_KEYS) * 3
    attempt = 0
    while attempt < max_attempts:
        num, current_key = get_current_key_info()
        p = dict(params)
        p['key'] = current_key
        try:
            resp = requests.get(f'https://www.googleapis.com/youtube/v3/{endpoint}', params=p, timeout=30)
            if resp.status_code == 200:
                switch_count = 0
                return resp.json(), None
            elif resp.status_code == 403 and 'quotaExceeded' in resp.text:
                logging.warning(f"❌ Ключ {num} - квота исчерпана")
                switch_to_next_key()
                attempt += 1
                if switch_count > 0 and switch_count % len(API_KEYS) == 0:
                    time.sleep(60)
                continue
            else:
                return None, f'YouTube API error: {resp.status_code}'
        except Exception as e:
            return None, str(e)
    return None, 'All API keys quota exceeded'

# ===== ИЗВЛЕЧЕНИЕ VIDEO ID ИЗ ССЫЛКИ =====
def extract_video_id(url):
    m = re.search(r'(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([a-zA-Z0-9_-]{11})', url)
    if m:
        return m.group(1)
    # fallback — хэш от самой ссылки, если не смогли распарсить id
    return hashlib.md5(url.encode()).hexdigest()[:16]

# ===== ХРАНИЛИЩЕ ГОТОВЫХ ФАЙЛОВ (кэш по video_id + результаты zip/merge) =====
STORAGE_DIR = '/tmp/converted-files'
os.makedirs(STORAGE_DIR, exist_ok=True)

def files_url(filename):
    return request.host_url.rstrip('/') + f"/files/{filename}"

# ===== ПРОВАЙДЕР: notube.net =====
NOTUBE_SERVERS = ['s43', 's56', 's75']
NOTUBE_HEADERS = {
    'Origin': 'https://notube.net',
    'Referer': 'https://notube.net/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
}

def download_via_notube(video_url, output_path):
    for server in NOTUBE_SERVERS:
        base = f'https://{server}.notube.net'
        try:
            resp = requests.post(
                f'{base}/recover_weight.php',
                data={'url': video_url, 'format': 'mp3', 'lang': 'ru', 'subscribed': 'false'},
                headers=NOTUBE_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                logging.warning(f"notube ({server}): recover_weight статус {resp.status_code}")
                continue
            data = resp.json()
            token = data.get('token')
            name_mp4 = data.get('name_mp4')
            if not token:
                logging.warning(f"notube ({server}): нет token в ответе — {data}")
                continue

            requests.post(
                f'{base}/recover_file.php',
                data={
                    'url': video_url, 'format': 'mp3', 'name_mp4': name_mp4 or '',
                    'lang': 'ru', 'token': token, 'subscribed': 'false',
                    'playlist': 'false', 'adblock': 'false',
                },
                headers=NOTUBE_HEADERS,
                timeout=15,
            )

            page = requests.get(
                f'https://notube.net/ru/download?token={token}',
                headers=NOTUBE_HEADERS,
                timeout=20,
            )
            match = re.search(r'id="downloadButton"[^>]*href="([^"]+)"', page.text)
            if not match:
                logging.warning(f"notube ({server}): не нашли downloadButton в HTML")
                continue
            direct_link = match.group(1).replace('&amp;', '&')

            file_resp = requests.get(direct_link, headers=NOTUBE_HEADERS, timeout=60, stream=True)
            if file_resp.status_code != 200:
                logging.warning(f"notube ({server}): статус {file_resp.status_code} при скачивании файла")
                continue

            with open(output_path, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logging.info(f"notube ({server}): успешно скачан {os.path.basename(output_path)}")
            return True

        except Exception as e:
            logging.warning(f"notube ({server}) ошибка: {e}")
            continue

    return False

# ===== КЭШИРУЮЩЕЕ СКАЧИВАНИЕ ПО video_id =====
# Если трек уже качали (через /convert, /zip или /merge) — переиспользуем
# файл с диска, не ходим в notube повторно.
def get_or_download_track(video_url):
    video_id = extract_video_id(video_url)
    mp3_path = os.path.join(STORAGE_DIR, f'{video_id}.mp3')
    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
        return mp3_path, video_id

    for attempt in range(2):
        if download_via_notube(video_url, mp3_path):
            return mp3_path, video_id
        time.sleep(2)

    return None, video_id

# ===== ЭНДПОИНТ /convert (получить прямую ссылку на один трек) =====
@app.route('/convert', methods=['GET', 'OPTIONS'])
def convert():
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    mp3_path, video_id = get_or_download_track(url)
    if not mp3_path:
        return jsonify({'error': 'Не удалось скачать трек'}), 500

    return jsonify({'url': files_url(f'{video_id}.mp3'), 'video_id': video_id})

# ===== ЭНДПОИНТ /video-info (метаданные одиночного видео: название + превью) =====
@app.route('/video-info', methods=['GET'])
def video_info():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    video_id = extract_video_id(url)
    data, error = youtube_api_get('videos', {'part': 'snippet', 'id': video_id})
    if error:
        return jsonify({'error': error}), 500

    items = data.get('items', [])
    if not items:
        return jsonify({'error': 'Video not found'}), 404

    snippet = items[0]['snippet']
    thumbnail = snippet.get('thumbnails', {}).get('medium', {}).get('url') \
        or snippet.get('thumbnails', {}).get('default', {}).get('url', '')

    return jsonify({
        'title': snippet.get('title', 'Unknown'),
        'thumbnail': thumbnail,
        'video_id': video_id,
    })

# ===== ЭНДПОИНТ /files/<filename> =====
@app.route('/files/<path:filename>', methods=['GET'])
def serve_file(filename):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(STORAGE_DIR, safe_name)
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found (возможно, сервер перезапускался)'}), 404
    mimetype = 'application/zip' if safe_name.endswith('.zip') else 'audio/mpeg'
    return send_file(file_path, mimetype=mimetype, as_attachment=False)

# ===== ЭНДПОИНТ /playlist =====
@app.route('/playlist', methods=['GET'])
def playlist():
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

    data, error = youtube_api_get('playlistItems', {
        'part': 'snippet', 'maxResults': 50, 'playlistId': playlist_id
    })
    if error:
        return jsonify({'error': error}), 500

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

    return jsonify({'playlist': playlist_title, 'tracks': tracks, 'total': len(tracks)})

# ===== ФОНОВЫЕ ЗАДАЧИ (ZIP/MERGE) =====
jobs = {}
jobs_lock = threading.Lock()

def create_job(tracks):
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            'status': 'pending',
            'total': len(tracks),
            # Список треков с их состоянием — фронтенд опрашивает и обновляет
            # кнопки по каждому конкретному треку, как только он готов.
            'tracks': [
                {
                    'idx': idx,
                    'video_id': extract_video_id(t['url']),
                    'title': t.get('title', ''),
                    'done': False,
                    'url': None,
                }
                for idx, t in enumerate(tracks)
            ],
            'result_url': None,
            'error': None,
        }
    return job_id

def update_job(job_id, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)

def mark_track_done(job_id, idx, url):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['tracks'][idx]['done'] = True
            jobs[job_id]['tracks'][idx]['url'] = url

def run_zip_job(job_id, tracks):
    try:
        downloaded = [None] * len(tracks)

        def job(idx, track):
            mp3_path, video_id = get_or_download_track(track['url'])
            if mp3_path:
                mark_track_done(job_id, idx, files_url_static(f'{video_id}.mp3'))
            return idx, track.get('title', f'track_{idx}'), mp3_path

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(job, idx, track) for idx, track in enumerate(tracks)]
            for future in as_completed(futures):
                idx, title, path = future.result()
                if path:
                    downloaded[idx] = (title, path)
                else:
                    logging.warning(f"Не удалось скачать: {title}")

        downloaded = [d for d in downloaded if d is not None]
        if not downloaded:
            update_job(job_id, status='error', error='No MP3 files downloaded')
            return

        zip_name = f'{job_id}.zip'
        zip_path = os.path.join(STORAGE_DIR, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for i, (title, path) in enumerate(downloaded):
                filename = f"{i+1:02d} - {title}.mp3"
                safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename)
                zipf.write(path, safe_filename)

        update_job(job_id, status='done', result_url=files_url_static(zip_name))
    except Exception as e:
        logging.error(f"ZIP job error: {str(e)}")
        update_job(job_id, status='error', error=str(e))

def run_merge_job(job_id, tracks):
    try:
        downloaded = [None] * len(tracks)

        def job(idx, track):
            mp3_path, video_id = get_or_download_track(track['url'])
            if mp3_path:
                mark_track_done(job_id, idx, files_url_static(f'{video_id}.mp3'))
            return idx, mp3_path

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(job, idx, track) for idx, track in enumerate(tracks)]
            for future in as_completed(futures):
                idx, path = future.result()
                if path:
                    downloaded[idx] = path
                else:
                    logging.warning(f"Не удалось скачать трек #{idx}")

        mp3_files = [p for p in downloaded if p is not None]
        if not mp3_files:
            update_job(job_id, status='error', error='No MP3 files downloaded')
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            list_path = os.path.join(tmpdir, 'list.txt')
            with open(list_path, 'w') as f:
                for mp3 in mp3_files:
                    # Абсолютный путь — файлы лежат в STORAGE_DIR, не в tmpdir
                    escaped = mp3.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            merged_name = f'{job_id}.mp3'
            output_path = os.path.join(STORAGE_DIR, merged_name)
            cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logging.error(f"FFmpeg error: {result.stderr}")
                update_job(job_id, status='error', error='FFmpeg merge failed')
                return

            update_job(job_id, status='done', result_url=files_url_static(merged_name))
    except Exception as e:
        logging.error(f"Merge job error: {str(e)}")
        update_job(job_id, status='error', error=str(e))

# request.host_url недоступен вне контекста запроса (в фоновом потоке) —
# поэтому берём базовый адрес один раз из переменной окружения/константы.
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', 'https://yt-2-mp3.relaxdev.ru')

def files_url_static(filename):
    return PUBLIC_BASE_URL.rstrip('/') + f"/files/{filename}"

@app.route('/zip/start', methods=['POST'])
def zip_start():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400
    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    job_id = create_job(tracks)
    threading.Thread(target=run_zip_job, args=(job_id, tracks), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/merge/start', methods=['POST'])
def merge_start():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400
    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    job_id = create_job(tracks)
    threading.Thread(target=run_merge_job, args=(job_id, tracks), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/job/<job_id>/status', methods=['GET'])
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)

# ===== ДИАГНОСТИКА: тест notube напрямую =====
@app.route('/test-notube', methods=['GET'])
def test_notube():
    test_video_url = request.args.get('url', 'https://www.youtube.com/watch?v=j3i_-mTVkZk')
    start = time.time()
    tmp_out = os.path.join(tempfile.gettempdir(), f'test-{uuid.uuid4()}.mp3')
    ok = download_via_notube(test_video_url, tmp_out)
    elapsed = round(time.time() - start, 2)
    if not ok:
        return jsonify({'ok': False, 'elapsed_seconds': elapsed, 'error': 'Все сервера notube не сработали'})
    size = os.path.getsize(tmp_out)
    os.remove(tmp_out)
    return jsonify({'ok': True, 'elapsed_seconds': elapsed, 'mp3_size_bytes': size})

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080, threaded=True)
    print("Server started")
