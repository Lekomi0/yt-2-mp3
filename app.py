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

# ===== ПРОВАЙДЕР: notube.net =====
# Известные рабочие сервера-зеркала — если один недоступен, пробуем следующий.
NOTUBE_SERVERS = ['s43', 's56', 's75']

NOTUBE_HEADERS = {
    'Origin': 'https://notube.net',
    'Referer': 'https://notube.net/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
}

def download_via_notube(video_url, output_dir, filename_base):
    for server in NOTUBE_SERVERS:
        base = f'https://{server}.notube.net'
        try:
            # Шаг 1: получить token + имя файла
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

            # Шаг 2: подтвердить формат/запустить подготовку файла
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

            # Шаг 3: получить страницу с готовой ссылкой на скачивание
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

            # Шаг 4: скачать сам файл
            file_resp = requests.get(direct_link, headers=NOTUBE_HEADERS, timeout=60, stream=True)
            if file_resp.status_code != 200:
                logging.warning(f"notube ({server}): статус {file_resp.status_code} при скачивании файла")
                continue

            mp3_path = os.path.join(output_dir, f'{filename_base}.mp3')
            with open(mp3_path, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logging.info(f"notube ({server}): успешно скачан {filename_base}")
            return mp3_path

        except Exception as e:
            logging.warning(f"notube ({server}) ошибка: {e}")
            continue

    return None

# ===== ГЛАВНАЯ ФУНКЦИЯ СКАЧИВАНИЯ =====
# Сейчас единственный провайдер — notube. Когда добавим ещё сайты (например
# hinoter.com), сюда добавится "гонка" через race_providers().
def download_audio(video_url, output_dir, filename_base, retries=2):
    for attempt in range(retries):
        result = download_via_notube(video_url, output_dir, filename_base)
        if result:
            return result
        time.sleep(2)
    return None

# ===== ХРАНИЛИЩЕ ДЛЯ ГОТОВЫХ ФАЙЛОВ (для /convert + /files) =====
STORAGE_DIR = '/tmp/converted-files'
os.makedirs(STORAGE_DIR, exist_ok=True)

# ===== ЭНДПОИНТ /convert =====
@app.route('/convert', methods=['GET', 'OPTIONS'])
def convert():
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    file_id = str(uuid.uuid4())
    mp3_path = download_audio(url, STORAGE_DIR, file_id)
    if not mp3_path:
        return jsonify({'error': 'Не удалось скачать трек'}), 500

    final_name = f"{file_id}.mp3"
    final_path = os.path.join(STORAGE_DIR, final_name)
    if mp3_path != final_path:
        os.rename(mp3_path, final_path)

    direct_url = request.host_url.rstrip('/') + f"/files/{final_name}"
    return jsonify({'url': direct_url})

# ===== ЭНДПОИНТ /files/<filename> =====
@app.route('/files/<path:filename>', methods=['GET'])
def serve_file(filename):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(STORAGE_DIR, safe_name)
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found (возможно, сервер перезапускался)'}), 404
    return send_file(file_path, mimetype='audio/mpeg', as_attachment=False)

# ===== ЭНДПОИНТ /download (скачать один трек — отдаёт mp3 файлом) =====
@app.route('/download', methods=['GET', 'OPTIONS'])
def download():
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        mp3_path = download_audio(url, tmpdir, 'track')
        if not mp3_path:
            return jsonify({'error': 'Не удалось скачать трек'}), 500
        return send_file(mp3_path, as_attachment=True, download_name='track.mp3')

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

# ===== ФОНОВЫЕ ЗАДАЧИ (ZIP/MERGE) =====
jobs = {}
jobs_lock = threading.Lock()

def create_job(total):
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            'status': 'pending',
            'completed': 0,
            'total': total,
            'file_path': None,
            'download_name': None,
            'error': None,
        }
    return job_id

def update_job(job_id, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)

def increment_job_progress(job_id):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]['completed'] += 1

def run_zip_job(job_id, tracks):
    tmpdir = tempfile.mkdtemp()
    try:
        downloaded = [None] * len(tracks)

        def job(idx, track):
            filename_base = f"track_{idx:04d}"
            path = download_audio(track['url'], tmpdir, filename_base)
            increment_job_progress(job_id)
            return idx, track.get('title', filename_base), path

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

        zip_path = os.path.join(tmpdir, 'playlist.zip')
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for i, (title, path) in enumerate(downloaded):
                filename = f"{i+1:02d} - {title}.mp3"
                safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename)
                zipf.write(path, safe_filename)

        update_job(job_id, status='done', file_path=zip_path, download_name='playlist.zip')
    except Exception as e:
        logging.error(f"ZIP job error: {str(e)}")
        update_job(job_id, status='error', error=str(e))

def run_merge_job(job_id, tracks):
    tmpdir = tempfile.mkdtemp()
    try:
        downloaded = [None] * len(tracks)

        def job(idx, track):
            filename_base = f"track_{idx:04d}"
            path = download_audio(track['url'], tmpdir, filename_base)
            increment_job_progress(job_id)
            return idx, path

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

        list_path = os.path.join(tmpdir, 'list.txt')
        with open(list_path, 'w') as f:
            for mp3 in mp3_files:
                f.write(f"file '{os.path.basename(mp3)}'\n")

        output_path = os.path.join(tmpdir, 'merged.mp3')
        cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir, timeout=300)
        if result.returncode != 0:
            logging.error(f"FFmpeg error: {result.stderr}")
            update_job(job_id, status='error', error='FFmpeg merge failed')
            return

        update_job(job_id, status='done', file_path=output_path, download_name='merged.mp3')
    except Exception as e:
        logging.error(f"Merge job error: {str(e)}")
        update_job(job_id, status='error', error=str(e))

@app.route('/zip/start', methods=['POST'])
def zip_start():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400
    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    job_id = create_job(len(tracks))
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

    job_id = create_job(len(tracks))
    threading.Thread(target=run_merge_job, args=(job_id, tracks), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/job/<job_id>/status', methods=['GET'])
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status': job['status'],
        'completed': job['completed'],
        'total': job['total'],
        'error': job['error'],
    })

@app.route('/job/<job_id>/download', methods=['GET'])
def job_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'done':
        return jsonify({'error': 'Job not finished yet'}), 400
    return send_file(job['file_path'], as_attachment=True, download_name=job['download_name'])

# ===== ДИАГНОСТИКА: тест notube напрямую =====
@app.route('/test-notube', methods=['GET'])
def test_notube():
    test_video_url = request.args.get('url', 'https://www.youtube.com/watch?v=j3i_-mTVkZk')
    start = time.time()
    with tempfile.TemporaryDirectory() as tmpdir:
        mp3_path = download_via_notube(test_video_url, tmpdir, 'test')
        elapsed = round(time.time() - start, 2)
        if not mp3_path:
            return jsonify({'ok': False, 'elapsed_seconds': elapsed, 'error': 'Все сервера notube не сработали'})
        size = os.path.getsize(mp3_path)
        return jsonify({'ok': True, 'elapsed_seconds': elapsed, 'mp3_size_bytes': size})

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080, threaded=True)
    print("Server started")
