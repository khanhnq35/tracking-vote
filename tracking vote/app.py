from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import json
from datetime import datetime
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
import os

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Biến lưu trữ dữ liệu vote
vote_data = {
    'last_update': None,
    'candidates': []
}

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_vote_data():
    try:
        url = "https://yvoting-service.onfan.vn/api/v1/nominations?awardId=49afae89-7cba-481b-9049-28d76f4a2ea8"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()

        candidates = []
        for item in data["data"]:
            name = item["character"]["name"]
            percent = round(item["ratioVotes"], 2)
            candidates.append({
                "name": name,
                "percent": percent
            })

        vote_data['last_update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        vote_data['candidates'] = candidates

        # Lưu lịch sử vào file history.json
        history_item = {
            'timestamp': vote_data['last_update'],
            'candidates': candidates
        }
        history_path = 'history.json'
        try:
            if os.path.exists(history_path):
                with open(history_path, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            else:
                history = []
            history.append(history_item)
            with open(history_path, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Lỗi lưu lịch sử: {e}")

    except Exception as e:
        print(f"Lỗi: {e}")

# Khởi tạo scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=fetch_vote_data, trigger="interval", minutes=10)
scheduler.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/vote-data')
def get_vote_data():
    return jsonify(vote_data)

@app.route('/api/history')
def get_history():
    # Lấy tham số từ query string
    interval = request.args.get('interval', '1d')  # mặc định 1 ngày
    candidates_filter = request.args.getlist('candidates')  # danh sách tên ứng viên cần theo dõi

    # Đọc dữ liệu lịch sử
    history_path = 'history.json'
    if not os.path.exists(history_path):
        return jsonify([])
    with open(history_path, 'r', encoding='utf-8') as f:
        history = json.load(f)

    # Xử lý khoảng thời gian
    now = datetime.now()
    interval_map = {
        '10m': 10*60,
        '1h': 60*60,
        '5h': 5*60*60,
        '1d': 24*60*60,
        '3d': 3*24*60*60,
        '7d': 7*24*60*60
    }
    seconds = interval_map.get(interval, 24*60*60)
    filtered_history = []
    for item in history:
        try:
            ts = datetime.strptime(item['timestamp'], "%Y-%m-%d %H:%M:%S")
            if (now - ts).total_seconds() <= seconds:
                # Lọc theo ứng viên nếu có
                if candidates_filter:
                    filtered_candidates = [c for c in item['candidates'] if c['name'] in candidates_filter]
                else:
                    filtered_candidates = item['candidates']
                filtered_history.append({
                    'timestamp': item['timestamp'],
                    'candidates': filtered_candidates
                })
        except Exception as e:
            continue
    # Sắp xếp lại lịch sử theo thời gian giảm dần (mới nhất lên trên)
    filtered_history.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify(filtered_history)

if __name__ == '__main__':
    try:
        # Fetch data lần đầu khi khởi động
        fetch_vote_data()
        app.run(debug=True)
    except Exception as e:
        logger.error(f"Lỗi khởi động ứng dụng: {str(e)}") 