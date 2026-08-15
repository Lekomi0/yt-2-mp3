from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import time
import logging
import os
import re
import zipfile
import tempfile
import yt_dlp
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

# ===== СКАЧИВАНИЕ АУДИО ЧЕРЕЗ yt-dlp =====
# Качает видео напрямую с YouTube и сразу конвертирует в mp3 через ffmpeg
# (постпроцессор yt-dlp), без сторонних сервисов.
# ===== COOKIES ДЛЯ ОБХОДА "Sign in to confirm you're not a bot" =====
# Render Secret Files монтируются по пути /etc/secrets/<имя файла> и он READ-ONLY.
# yt-dlp в конце пытается перезаписать файл с куками (YouTube может их
# ротировать) — поэтому копируем в writable-копию во временной директории,
# а не отдаём yt-dlp путь до read-only секрета напрямую.
import shutil

_COOKIES_SOURCE = os.getenv('COOKIES_FILE_PATH', '/etc/secrets/cookies.txt')
COOKIES_FILE = None
if os.path.exists(_COOKIES_SOURCE):
    _writable_cookies = os.path.join(tempfile.gettempdir(), 'cookies_writable.txt')
    shutil.copy(_COOKIES_SOURCE, _writable_cookies)
    COOKIES_FILE = _writable_cookies
    logging.info(f"Найден cookies-файл: {_COOKIES_SOURCE} → скопирован в {COOKIES_FILE}")
else:
    logging.warning(f"Cookies-файл не найден ({_COOKIES_SOURCE}) — будем качать без авторизации")

def download_audio(video_url, output_dir, filename_base, retries=3):
    output_template = os.path.join(output_dir, f"{filename_base}.%(ext)s")
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'socket_timeout': 30,
        # android не поддерживает cookies (yt-dlp его просто пропускает),
        # поэтому используем web — единственный клиент, совместимый с
        # куками. Для решения его подписи нужен JS-движок (Deno, см.
        # Dockerfile) — без него web отдаёт только превью-картинки.
        'extractor_args': {
            'youtube': {
                'player_client': ['web'],
            }
        },
        # Небольшая пауза между запросами снижает шанс словить бот-проверку
        'sleep_interval_requests': 1,
    }
    if COOKIES_FILE:
        ydl_opts['cookiefile'] = COOKIES_FILE

    mp3_path = os.path.join(output_dir, f"{filename_base}.mp3")

    for attempt in range(retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
            if os.path.exists(mp3_path):
                return mp3_path
        except Exception as e:
            logging.warning(f"yt-dlp попытка {attempt+1} для {video_url}: {e}")
            time.sleep(2 * (attempt + 1))

    return None

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
# Большие плейлисты качаются минутами — держать HTTP-запрос открытым всё
# это время ненадёжно (таймауты у хостера/браузера рвут соединение).
# Поэтому запускаем скачивание в фоновом потоке, а фронтенд опрашивает
# статус и забирает готовый файл отдельным запросом.
import uuid
import threading
import subprocess

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

        with ThreadPoolExecutor(max_workers=1) as executor:
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

        with ThreadPoolExecutor(max_workers=1) as executor:
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

# ===== ЭНДПОИНТ /zip/start (запускает фоновую задачу, сразу отдаёт job_id) =====
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

# ===== ЭНДПОИНТ /merge/start (аналогично, для склейки) =====
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

# ===== ЭНДПОИНТ /job/<id>/status (опрос прогресса) =====
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

# ===== ЭНДПОИНТ /job/<id>/download (забрать готовый файл) =====
@app.route('/job/<job_id>/download', methods=['GET'])
def job_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'done':
        return jsonify({'error': 'Job not finished yet'}), 400
    return send_file(job['file_path'], as_attachment=True, download_name=job['download_name'])

# ===== ДИАГНОСТИКА: тест yt-dlp прямо на сервере =====
@app.route('/test-convert1s', methods=['GET'])
def test_convert1s():
    headers = {
        'accept': 'application/json',
        'content-type': 'application/json',
        'origin': 'https://media.ytmp3.gg',
        'referer': 'https://media.ytmp3.gg/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }
    payload = {
        "url": "https://www.youtube.com/watch?v=j3i_-mTVkZk",
        "os": "windows",
        "output": {"type": "audio", "format": "mp3"},
        "audio": {"bitrate": "128k"}
    }
    start = time.time()
    try:
        resp = requests.post('https://hub.convert1s.com/api/download', json=payload, headers=headers, timeout=15)
        elapsed = round(time.time() - start, 2)
        return jsonify({
            'ok': resp.status_code == 200,
            'status_code': resp.status_code,
            'elapsed_seconds': elapsed,
            'body_preview': resp.text[:500],
        })
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        return jsonify({
            'ok': False,
            'elapsed_seconds': elapsed,
            'error': str(e),
        })

@app.route('/test-ytdlp', methods=['GET'])
def test_ytdlp():
    import subprocess
    test_video_url = request.args.get('url', 'https://www.youtube.com/watch?v=j3i_-mTVkZk')

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, 'test.%(ext)s')
        cmd = [
            'yt-dlp', '-x', '--audio-format', 'mp3',
            '--extractor-args', 'youtube:player_client=web',
            '-o', output_template,
        ]
        if COOKIES_FILE:
            cmd += ['--cookies', COOKIES_FILE]
        cmd.append(test_video_url)
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            return jsonify({
                'ok': False,
                'error': 'timeout после 60 секунд — похоже на троттлинг/блокировку',
                'stdout': (e.stdout or b'').decode(errors='ignore')[-2000:] if e.stdout else '',
                'stderr': (e.stderr or b'').decode(errors='ignore')[-2000:] if e.stderr else '',
            }), 200
        elapsed = round(time.time() - start, 2)

        files = os.listdir(tmpdir)
        mp3_size = None
        for f in files:
            if f.endswith('.mp3'):
                mp3_size = os.path.getsize(os.path.join(tmpdir, f))

        return jsonify({
            'ok': result.returncode == 0 and mp3_size is not None,
            'cookies_used': COOKIES_FILE is not None,
            'elapsed_seconds': elapsed,
            'returncode': result.returncode,
            'mp3_size_bytes': mp3_size,
            'stdout_tail': result.stdout[-1500:],
            'stderr_tail': result.stderr[-1500:],
        })

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080, threaded=True)
    print("Server started")
