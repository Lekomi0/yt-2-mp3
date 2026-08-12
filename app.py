from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import time
import logging
import os
import re
from requests.exceptions import ConnectionError

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ===== ЭНДПОИНТ /download =====
@app.route('/download', methods=['GET', 'OPTIONS'])
def download():
    if request.method == 'OPTIONS':
        return '', 200

    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    logging.info(f"Получен URL: {url}")

    cache_buster = int(time.time() * 1000)

    configs = [
        {"api": "convert1s", "bitrate": "320k", "timeout": 20, "attempts": 2, "delay": 3},
        {"api": "convert1s", "bitrate": "128k", "timeout": 20, "attempts": 5, "delay": 3}
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
                    payload = {
                        "url": url,
                        "os": "windows",
                        "output": {"type": "audio", "format": "mp3"},
                        "audio": {"bitrate": config["bitrate"]},
                        "_": cache_buster + attempt
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
                    for _ in range(20):
                        time.sleep(2)
                        try:
                            status_resp = requests.get(status_url, timeout=20)
                            if status_resp.status_code != 200:
                                continue
                            status_data = status_resp.json()
                            if 'downloadUrl' in status_data and status_data['downloadUrl']:
                                mp3_url = status_data['downloadUrl']
                                logging.info(f"Получена ссылка через convert1s ({config['bitrate']}) попытка {attempt+1}: {mp3_url}")
                                return jsonify({'link': mp3_url})
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

    return jsonify({'error': 'Conversion failed after all attempts'}), 500


# ===== ЭНДПОИНТ /playlist (с ретраями и сохранением частичного результата) =====
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

    API_KEY = os.getenv('YOUTUBE_API_KEY')
    if not API_KEY:
        return jsonify({'error': 'YouTube API key not configured'}), 500

    all_tracks = []
    next_page_token = None
    max_results_per_page = 50
    playlist_title = 'YouTube Playlist'
    page_count = 0
    max_page_retries = 3

    try:
        while True:
            page_count += 1
            logging.info(f"Fetching page {page_count} with token: {next_page_token}")

            params = {
                'part': 'snippet',
                'maxResults': max_results_per_page,
                'playlistId': playlist_id,
                'key': API_KEY
            }
            if next_page_token:
                params['pageToken'] = next_page_token

            page_data = None
            last_error = None

            for attempt in range(max_page_retries):
                try:
                    resp = requests.get(
                        'https://www.googleapis.com/youtube/v3/playlistItems',
                        params=params,
                        timeout=(5, 60)
                    )
                    if resp.status_code == 200:
                        page_data = resp.json()
                        break
                    else:
                        last_error = f'YouTube API error: {resp.status_code} - {resp.text[:200]}'
                        logging.warning(f"Page {page_count} attempt {attempt+1}: {last_error}")
                        time.sleep(2)
                except Exception as e:
                    last_error = str(e)
                    logging.warning(f"Page {page_count} attempt {attempt+1}: exception {last_error}")
                    time.sleep(2)

            if page_data is None:
                logging.error(f"Page {page_count} failed after {max_page_retries} attempts: {last_error}")
                if all_tracks:
                    return jsonify({
                        'playlist': playlist_title,
                        'tracks': all_tracks,
                        'partial': True,
                        'warning': f'Не удалось загрузить страницу {page_count}, собрано {len(all_tracks)} треков из плейлиста. Ошибка: {last_error}'
                    })
                else:
                    return jsonify({'error': last_error or 'Unknown error fetching playlist'}), 500

            logging.info(f"Page {page_count} returned {len(page_data.get('items', []))} items")

            if page_count == 1 and page_data.get('items'):
                first_item = page_data['items'][0]
                playlist_title = first_item.get('snippet', {}).get('playlistTitle', 'YouTube Playlist')

            for item in page_data.get('items', []):
                snippet = item.get('snippet', {})
                video_id = snippet.get('resourceId', {}).get('videoId')
                if video_id:
                    all_tracks.append({
                        'title': snippet.get('title', 'Unknown'),
                        'id': video_id,
                        'url': f"https://www.youtube.com/watch?v={video_id}"
                    })

            next_page_token = page_data.get('nextPageToken')
            if not next_page_token:
                break

        logging.info(f"Total tracks collected: {len(all_tracks)}")
        if not all_tracks:
            return jsonify({'error': 'No tracks found'}), 404

        return jsonify({
            'playlist': playlist_title,
            'tracks': all_tracks,
            'total': len(all_tracks)
        })

    except Exception as e:
        logging.error(f"Playlist error: {str(e)}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
