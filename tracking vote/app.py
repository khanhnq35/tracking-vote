from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import json
from datetime import datetime, timedelta
import logging
from tenacity import retry, stop_after_attempt, wait_exponential
import os
import pytz
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import desc

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

# Cấu hình Database
DATABASE_URL = os.environ.get('DATABASE_URL') # Lấy từ biến môi trường
if not DATABASE_URL:
    logger.error("DATABASE_URL is not set.")
    # Bạn có thể thêm xử lý lỗi hoặc giá trị mặc định tại đây nếu cần cho local dev
    # Ví dụ: DATABASE_URL = 'sqlite:///local_history.db' # SQLite cho local testing

engine = create_engine(DATABASE_URL)
Base = declarative_base()

# Định nghĩa Model (Bảng trong Database)
class VoteRecord(Base):
    __tablename__ = 'vote_history'

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, index=True)
    candidate_name = Column(String, index=True)
    percent = Column(Float)

    def __repr__(self):
        return f"<VoteRecord(timestamp='{self.timestamp}', name='{self.candidate_name}', percent={self.percent})>"

# Tạo bảng trong database nếu chưa tồn tại
Base.metadata.create_all(engine)

# Tạo Session maker
Session = sessionmaker(bind=engine)

# Biến lưu trữ dữ liệu vote (sẽ lấy từ DB cho lần request đầu)
vote_data = {
    'last_update': None,
    'candidates': []
}

# Hàm fetch data và lưu vào DB
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_vote_data():
    logger.info("Bắt đầu fetch dữ liệu từ yvoting-service API")
    session = Session()
    try:
        url = "https://yvoting-service.onfan.vn/api/v1/nominations?awardId=49afae89-7cba-481b-9049-28d76f4a2ea8"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=20) # Tăng timeout
        response.raise_for_status()

        data = response.json()
        current_time = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))

        candidates = []
        new_vote_records = []
        for item in data.get("data", []):
            try:
                name = item["character"]["name"]
                percent = round(item["ratioVotes"], 2)
                candidates.append({
                    "name": name,
                    "percent": percent
                })
                # Tạo bản ghi mới để lưu vào DB
                new_vote_records.append(VoteRecord(
                    timestamp=current_time,
                    candidate_name=name,
                    percent=percent
                ))
            except KeyError as e:
                logger.error(f"Missing key in API response item: {e}. Item: {item}")
                continue # Bỏ qua item bị thiếu key
            except Exception as e:
                logger.error(f"Lỗi khi xử lý item từ API: {e}. Item: {item}")
                continue # Bỏ qua item bị lỗi

        # Thêm các bản ghi mới vào session và commit vào DB
        if new_vote_records:
            session.add_all(new_vote_records)
            session.commit()
            logger.info(f"Đã lưu {len(new_vote_records)} bản ghi vote mới vào database tại {current_time}")
        else:
             logger.warning("API returned no data or failed to parse.")

        # Cập nhật vote_data với dữ liệu mới nhất fetch được (tùy chọn, có thể bỏ qua nếu luôn đọc từ DB)
        # vote_data['last_update'] = current_time.strftime("%Y-%m-%d %H:%M:%S")
        # vote_data['candidates'] = candidates

    except requests.RequestException as e:
        logger.error(f"Lỗi kết nối hoặc HTTP khi fetch data: {e}")
        session.rollback() # Rollback nếu có lỗi
        raise
    except json.JSONDecodeError as e:
         logger.error(f"Lỗi parse JSON từ API: {e}")
         session.rollback() # Rollback nếu có lỗi
         raise
    except Exception as e:
        logger.error(f"Lỗi không xác định khi fetch data: {e}")
        session.rollback() # Rollback nếu có lỗi
        raise
    finally:
        session.close() # Luôn đóng session

# API trả về dữ liệu vote hiện tại
@app.route('/api/vote-data')
def get_vote_data():
    session = Session()
    try:
        # Lấy dữ liệu mới nhất cho mỗi ứng viên từ DB
        latest_timestamp_query = session.query(VoteRecord.timestamp).order_by(desc(VoteRecord.timestamp)).limit(1).scalar()
        
        if not latest_timestamp_query:
             return jsonify({'last_update': None, 'candidates': []})
             
        latest_records = session.query(VoteRecord).filter_by(timestamp=latest_timestamp_query).all()

        candidates = [{
            'name': record.candidate_name,
            'percent': record.percent
        } for record in latest_records]
        
        last_update_str = latest_timestamp_query.strftime("%Y-%m-%d %H:%M:%S")

        return jsonify({'last_update': last_update_str, 'candidates': candidates})
    except Exception as e:
        logger.error(f"Lỗi khi lấy dữ liệu vote hiện tại từ DB: {e}")
        return jsonify({'last_update': None, 'candidates': [], 'error': str(e)}), 500
    finally:
        session.close()

# API trả về lịch sử
@app.route('/api/history')
def get_history():
    session = Session()
    try:
        interval = request.args.get('interval', '1d')
        candidates_filter = request.args.getlist('candidates')

        now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        interval_map = {
            '10m': 10 * 60,
            '1h': 60 * 60,
            '5h': 5 * 60 * 60,
            '1d': 24 * 60 * 60,
            '3d': 3 * 24 * 60 * 60,
            '7d': 7 * 24 * 60 * 60
        }
        seconds = interval_map.get(interval, 24 * 60 * 60)
        time_threshold = now - timedelta(seconds=seconds)

        query = session.query(VoteRecord).filter(VoteRecord.timestamp >= time_threshold)

        if candidates_filter:
            query = query.filter(VoteRecord.candidate_name.in_(candidates_filter))
            
        # Sắp xếp theo timestamp và candidate_name để dễ xử lý
        records = query.order_by(VoteRecord.timestamp, VoteRecord.candidate_name).all()
        
        # Gom nhóm bản ghi theo timestamp
        history_dict = {}
        for record in records:
            ts_str = record.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            if ts_str not in history_dict:
                history_dict[ts_str] = []
            history_dict[ts_str].append({
                'name': record.candidate_name,
                'percent': record.percent
            })
            
        # Chuyển dictionary thành list và sắp xếp theo timestamp giảm dần
        filtered_history = []
        for ts_str, candidates in history_dict.items():
            filtered_history.append({
                'timestamp': ts_str,
                'candidates': candidates
            })
            
        filtered_history.sort(key=lambda x: x['timestamp'], reverse=True)

        return jsonify(filtered_history)
    except Exception as e:
        logger.error(f"Lỗi khi lấy lịch sử từ DB: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


# Khởi tạo scheduler
# Sử dụng timezone từ pytz, ví dụ múi giờ Việt Nam (Asia/Ho_Chi_Minh)
scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Ho_Chi_Minh'))
scheduler.add_job(func=fetch_vote_data, trigger="interval", minutes=10)
scheduler.start()

# Fetch data lần đầu khi ứng dụng được import (để có dữ liệu ngay từ đầu)
fetch_vote_data() # Gọi ở đây thay vì trong if __name__ == '__main__':

if __name__ == '__main__':
    # Khi chạy local, có thể chạy app.run() ở đây
    # Khi deploy với Gunicorn, khối này sẽ không chạy
    try:
        # app.run(debug=True)
        # logger.info("Flask app is running locally...")
        pass # Gunicorn sẽ start app
    except Exception as e:
        logger.error(f"Lỗi khởi động ứng dụng (trong __main__): {str(e)}") 
