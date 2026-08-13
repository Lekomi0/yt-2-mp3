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

if not API_KEYS or not API_KEYS[0][1]:
    logging.warning("⚠️ No API keys configured - playlist feature disabled")

logging.info(f"Загружено {len(API_KEYS)} API ключей:")
for num, key in API_KEYS:
    if key:
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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
}

# ===== ФУНКЦИИ КОНВЕРТАЦИИ (рабочие сервисы) =====

def convert_via_ytmp3_cc(url):
    """Конвертирует через ytmp3.cc - основной сервис"""
    logging.info("🔄 Пытаемся ytmp3.cc...")
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        if PROXY:
            session.proxies = {'http': PROXY, 'https': PROXY}
        
        # Используем API ytmp3.cc
        resp = session.get(
            'https://ytmp3.cc/api/download',
            params={'url': url},
            timeout=30
        )
        
        if resp.status_code == 200:
            data = resp.json()
            
            if data.get('success') and data.get('download_url'):
                mp3_url = data['download_url']
                logging.info(f"✅ ytmp3.cc сработал! Ссылка: {mp3_url[:80]}...")
                return {'link': mp3_url}
            else:
                logging.warning(f"ytmp3.cc: {data.get('message', 'unknown error')}")
                return None
        else:
            logging.warning(f"ytmp3.cc: статус {resp.status_code}")
            return None
            
    except Exception as e:
        logging.warning(f"❌ ytmp3.cc ошибка: {e}")
        return None

def convert_via_ytmp3_cc_alt(url):
    """Альтернативный способ через ytmp3.cc"""
    logging.info("🔄 Пытаемся ytmp3.cc (способ 2)...")
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        if PROXY:
            session.proxies = {'http': PROXY, 'https': PROXY}
        
        # Парсим video ID
        video_id = re.search(r'v=([^&]+)', url)
        if not video_id:
            return None
        
        video_id = video_id.group(1)
        
        # Пытаемся через другой эндпоинт
        resp = session.get(
            f'https://ytmp3.cc/download/{video_id}',
            timeout=30,
            allow_redirects=True
        )
        
        if resp.status_code == 200:
            # Проверяем, что это MP3
            if 'audio' in resp.headers.get('content-type', ''):
                # Это прямая ссылка на MP3
                logging.info(f"✅ ytmp3.cc (способ 2) сработал!")
                return {'link': resp.url}
        
        return None
            
    except Exception as e:
        logging.warning(f"❌ ytmp3.cc (способ 2) ошибка: {e}")
        return None

def convert_via_cloudconvert_indirect(url):
    """Пытаемся через публичные API сервисы"""
    logging.info("🔄 Пытаемся через альтернативный сервис...")
    try:
        session = requests.Session()
        session.headers.update(COMMON_HEADERS)
        
        if PROXY:
            session.proxies = {'http': PROXY, 'https': PROXY}
        
        # Пытаемся несколько публичных API
        endpoints = [
            'https://api.audd.io/convert',
            'https://api-v2.soundtrap.com/convert',
        ]
        
        for endpoint in endpoints:
            try:
                resp = session.post(
                    endpoint,
                    json={'url': url, 'format': 'mp3'},
                    timeout=20
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('success') and data.get('url'):
                        logging.info(f"✅ Альтернативный сервис сработал!")
                        return {'link': data['url']}
            except:
                continue
        
        return None
            
    except Exception as e:
        logging.warning(f"❌ Альтернативный сервис ошибка: {e}")
        return None

def process_download(url):
    """Основная функция - пробует несколько сервисов конвертации"""
    logging.info(f"Получен URL: {url}")
    
    # Список сервисов для попытки (в порядке приоритета)
    # Используем только те, которые доступны из России!
    services = [
        {'name': 'ytmp3.cc (основной)', 'func': convert_via_ytmp3_cc},
        {'name': 'ytmp3.cc (способ 2)', 'func': convert_via_ytmp3_cc_alt},
        {'name': 'Альтернативные API', 'func': convert_via_cloudconvert_indirect},
    ]
    
    for service in services:
        try:
            logging.info(f"Пытаемся {service['name']}...")
            result = service['func'](url)
            
            if result and 'link' in result and result['link']:
                logging.info(f"✅ Сервис {service['name']} успешно получил ссылку")
                return result
            else:
                logging.info(f"⏭️  {service['name']} не сработал, пробуем следующий...")
                time.sleep(1)
                continue
                
        except Exception as e:
            logging.warning(f"❌ {service['name']} исключение: {e}")
            time.sleep(1)
            continue
    
    logging.error("❌ Все сервисы конвертации исчерпаны")
    return {'error': 'Conversion failed. Service unavailable. Try again later.'}

# ===== ДИАГНОСТИЧЕСКИЙ ЭНДПОИНТ =====
@app.route('/diagnostic', methods=['GET'])
def diagnostic():
    """Проверяет доступность сервисов конвертации"""
    results = {}
    
    services_to_check = [
        ('google.com', 'https://www.google.com'),
        ('youtube.com', 'https://www.youtube.com'),
        ('ytmp3.cc', 'https://ytmp3.cc'),
        ('ytmp3.cc API', 'https://ytmp3.cc/api/download?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ'),
    ]
    
    for name, url in services_to_check:
        try:
            resp = requests.get(url, timeout=10)
            results[name] = {
                'status': 'OK',
                'code': resp.status_code,
                'accessible': True
            }
        except requests.exceptions.ConnectionError as e:
            results[name] = {
                'status': 'CONNECTION_ERROR',
                'error': str(e)[:100],
                'accessible': False
            }
        except requests.exceptions.Timeout:
            results[name] = {
                'status': 'TIMEOUT',
                'accessible': False
            }
        except Exception as e:
            results[name] = {
                'status': 'ERROR',
                'error': str(e)[:100],
                'accessible': False
            }
    
    return jsonify(results)

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

    # Проверяем, есть ли API ключи
    if not API_KEYS or not API_KEYS[0][1]:
        return jsonify({'error': 'YouTube API keys not configured'}), 500

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

# ===== ФУНКЦИЯ ДЛЯ СКАЧИВАНИЯ MP3 =====
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

# ===== ЭНДПОИНТ /links (только получить ссылки, без скачивания файлов) =====
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

    ok_count = sum(1 for r in results if r and r.get('ok'))
    if ok_count == 0:
        return jsonify({'error': 'No MP3 links obtained'}), 500

    return jsonify({'tracks': results, 'total': len(tracks), 'ok': ok_count})

# ===== ЭНДПОИНТ /zip-upload =====
@app.route('/zip-upload', methods=['POST'])
def zip_upload():
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

# ===== ЭНДПОИНТ /merge-upload =====
@app.route('/merge-upload', methods=['POST'])
def merge_upload():
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

if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
