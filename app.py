from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import time
import logging
import os
import re
import subprocess
import json
from requests.exceptions import ConnectionError

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ===== ЭНДПОИНТ /download (через convert1s.com) =====
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


# ===== ЭНДПОИНТ /playlist (через yt-dlp-ytse с обходом блокировок) =====
@app.route('/playlist', methods=['GET'])
def playlist():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    logging.info(f"Received playlist URL: {url}")

    try:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            "--no-warnings",
            "--no-check-certificate",
            "-4",
            "--extractor-args", "youtube:player-client=web,default",
            "--playlist-end", "50",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            logging.error(f"yt-dlp error: {result.stderr}")
            return jsonify({'error': 'Failed to fetch playlist'}), 500

        tracks = []
        playlist_title = "YouTube Playlist"
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                if 'playlist_title' in data:
                    playlist_title = data['playlist_title']
                tracks.append({
                    'title': data.get('title', 'Unknown'),
                    'id': data.get('id'),
                    'url': f"https://www.youtube.com/watch?v={data.get('id')}"
                })
            except json.JSONDecodeError:
                continue

        if not tracks:
            return jsonify({'error': 'No tracks found'}), 404

        return jsonify({
            'playlist': playlist_title,
            'tracks': tracks,
            'total': len(tracks)
        })

    except subprocess.TimeoutExpired:
        logging.error("yt-dlp timeout")
        return jsonify({'error': 'Timeout fetching playlist'}), 500
    except Exception as e:
        logging.error(f"Playlist error: {str(e)}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("Starting server...")
    app.run(host='0.0.0.0', port=8080)
    print("Server started")
