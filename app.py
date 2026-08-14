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
        # YouTube агрессивно подозревает датацентровые IP в скрейпинге и
        # требует "Sign in to confirm you're not a bot". У Android-клиента
        # YouTube проверка мягче — притворяемся им.
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            }
        },
        # Небольшая пауза между запросами снижает шанс словить бот-проверку
        'sleep_interval_requests': 1,
    }

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

# ===== ЭНДПОИНТ /zip (скачивает все треки через yt-dlp и пакует в ZIP) =====
@app.route('/zip', methods=['POST'])
def zip_tracks():
    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            downloaded = [None] * len(tracks)

            def job(idx, track):
                filename_base = f"track_{idx:04d}"
                path = download_audio(track['url'], tmpdir, filename_base)
                return idx, track.get('title', filename_base), path

            # Небольшой параллелизм — YouTube тоже может троттлить при
            # слишком частых параллельных запросах с одного IP.
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(job, idx, track) for idx, track in enumerate(tracks)]
                for future in as_completed(futures):
                    idx, title, path = future.result()
                    if path:
                        downloaded[idx] = (title, path)
                    else:
                        logging.warning(f"Не удалось скачать: {title}")

            downloaded = [d for d in downloaded if d is not None]
            if not downloaded:
                return jsonify({'error': 'No MP3 files downloaded'}), 500

            zip_path = os.path.join(tmpdir, 'playlist.zip')
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for i, (title, path) in enumerate(downloaded):
                    filename = f"{i+1:02d} - {title}.mp3"
                    safe_filename = re.sub(r'[\\/*?:"<>|]', '', filename)
                    zipf.write(path, safe_filename)

            return send_file(zip_path, as_attachment=True, download_name='playlist.zip')
    except Exception as e:
        logging.error(f"ZIP error: {str(e)}")
        return jsonify({'error': str(e)}), 500

# ===== ЭНДПОИНТ /merge (скачивает все треки через yt-dlp и склеивает через ffmpeg) =====
@app.route('/merge', methods=['POST'])
def merge_tracks():
    import subprocess

    data = request.get_json()
    if not data or 'tracks' not in data:
        return jsonify({'error': 'Missing tracks list'}), 400

    tracks = data['tracks']
    if not tracks:
        return jsonify({'error': 'Empty tracks list'}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            downloaded = [None] * len(tracks)

            def job(idx, track):
                filename_base = f"track_{idx:04d}"
                path = download_audio(track['url'], tmpdir, filename_base)
                return idx, path

            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(job, idx, track) for idx, track in enumerate(tracks)]
                for future in as_completed(futures):
                    idx, path = future.result()
                    if path:
                        downloaded[idx] = path
                    else:
                        logging.warning(f"Не удалось скачать трек #{idx}")

            mp3_files = [p for p in downloaded if p is not None]
            if not mp3_files:
                return jsonify({'error': 'No MP3 files downloaded'}), 500

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

# ===== ДИАГНОСТИКА: тест yt-dlp прямо на сервере =====
@app.route('/test-ytdlp', methods=['GET'])
def test_ytdlp():
    import subprocess
    test_video_url = request.args.get('url', 'https://www.youtube.com/watch?v=j3i_-mTVkZk')

    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, 'test.%(ext)s')
        cmd = [
            'yt-dlp', '-x', '--audio-format', 'mp3',
            '--extractor-args', 'youtube:player_client=android,web',
            '-o', output_template,
            test_video_url
        ]
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
            'elapsed_seconds': elapsed,
            'returncode': result.returncode,
            'mp3_size_bytes': mp3_size,
            'stdout_tail': result.stdout[-1500:],
            'stderr_tail': result.stderr[-1500:],
        })

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
