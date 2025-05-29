from flask import Flask, render_template, jsonify, request, redirect, url_for
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
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_default_secret_key') # Lấy Secret Key từ biến môi trường hoặc dùng default

# Cấu hình Database
DATABASE_URL = os.environ.get('DATABASE_URL') # Lấy từ biến môi trường
if not DATABASE_URL:
    logger.error("DATABASE_URL is not set.")
    # Fallback cho local testing với SQLite nếu DATABASE_URL không có
    # DATABASE_URL = 'sqlite:///local_history.db'
    # engine = create_engine(DATABASE_URL)
    # Base.metadata.create_all(engine)
    # print("WARNING: Using SQLite for local development. Set DATABASE_URL for production.")
    raise ValueError("DATABASE_URL environment variable not set.")

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

# Định nghĩa Model User cho xác thực
class User(UserMixin, Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# Tạo bảng trong database nếu chưa tồn tại
Base.metadata.create_all(engine)

# Tạo Session maker
Session = sessionmaker(bind=engine)

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # Set the view function for the login page
login_manager.login_message_category = 'info'
login_manager.login_message = 'Vui lòng đăng nhập để truy cập đầy đủ lịch sử.'

# Thêm user mặc định nếu chưa tồn tại
def create_default_users():
    session = Session()
    try:
        users_to_create = [
            ('joeyadmin230705', '12345678'),
            ('p27nadmin121205', '12345678'),
            ('bevistdatadmin021006', '12345678')
        ]

        for username, password in users_to_create:
            existing_user = session.query(User).filter_by(username=username).first()
            if not existing_user:
                new_user = User(username=username)
                new_user.set_password(password)
                session.add(new_user)
                logger.info(f"Đã tạo người dùng: {username}")

        session.commit()
    except Exception as e:
        logger.error(f"Lỗi khi tạo người dùng mặc định: {e}")
        session.rollback()
    finally:
        session.close()

# Gọi hàm tạo user khi khởi động ứng dụng
create_default_users()

# User loader function required by Flask-Login
@login_manager.user_loader
def load_user(user_id):
    # Load user from DB based on user_id
    session = Session()
    try:
        # user_id ở đây là id (primary key), không phải username
        # Flask-Login lưu trữ user.get_id() là string
        # Cần chuyển về int nếu id là int
        user = session.query(User).get(int(user_id))
        return user
    except Exception as e:
        logger.error(f"Lỗi khi tải người dùng với id {user_id}: {e}")
        return None
    finally:
        session.close()

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
             
        # Lấy tất cả bản ghi tại timestamp mới nhất
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
@login_required # Yêu cầu đăng nhập để truy cập
def get_history():
    session = Session()
    try:
        interval = request.args.get('interval', '1d')
        candidates_filter = request.args.getlist('candidates')
        limit = request.args.get('limit', type=int) # Lấy tham số limit, ép kiểu sang int

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
            
        # Sắp xếp theo timestamp giảm dần
        query = query.order_by(desc(VoteRecord.timestamp))
        
        # Giới hạn bản ghi nếu user chưa đăng nhập
        if not current_user.is_authenticated:
             # Lấy 10 timestamp gần nhất trong khoảng thời gian đã chọn
             recent_timestamps = session.query(VoteRecord.timestamp).filter(VoteRecord.timestamp >= time_threshold).order_by(desc(VoteRecord.timestamp)).distinct().limit(10).subquery()
             query = query.filter(VoteRecord.timestamp.in_(recent_timestamps))
             logger.info("User not authenticated, limiting history to 10 recent timestamps.")
        elif limit is not None: # Áp dụng giới hạn nếu user đã đăng nhập và limit được cung cấp
            # Lấy các timestamp duy nhất giới hạn bởi limit
            recent_timestamps = session.query(VoteRecord.timestamp).filter(VoteRecord.timestamp >= time_threshold).order_by(desc(VoteRecord.timestamp)).distinct().limit(limit).subquery()
            query = query.filter(VoteRecord.timestamp.in_(recent_timestamps))
            logger.info(f"User authenticated, limiting history to {limit} recent timestamps.")
        else:
             logger.info("User authenticated, providing full history.")
             
        # Lấy bản ghi
        records = query.order_by(desc(VoteRecord.timestamp), VoteRecord.candidate_name).all()
        
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
            
        # Chuyển dictionary thành list và giữ nguyên thứ tự đã sắp xếp
        filtered_history = []
        # Lấy các timestamp duy nhất đã lọc và sắp xếp để duy trì thứ tự
        ordered_timestamps = sorted(history_dict.keys(), reverse=True)
        for ts_str in ordered_timestamps:
             filtered_history.append({
                 'timestamp': ts_str,
                 'candidates': history_dict[ts_str]
             })

        return jsonify(filtered_history)
    except Exception as e:
        logger.error(f"Lỗi khi lấy lịch sử từ DB: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

# Route cho trang đăng nhập
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index')) # Redirect nếu đã đăng nhập

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        session = Session()
        user = session.query(User).filter_by(username=username).first()
        session.close()

        if user and user.check_password(password):
            login_user(user) # Đăng nhập người dùng
            # Có thể thêm flash message thông báo đăng nhập thành công
            return redirect(url_for('index')) # Chuyển hướng đến trang chính sau khi đăng nhập
        else:
            # Có thể thêm flash message thông báo đăng nhập thất bại
            pass # Xử lý đăng nhập thất bại (ví dụ: hiển thị lại form với thông báo lỗi)

    # Render form đăng nhập (sẽ cần tạo file templates/login.html)
    return render_template('login.html')

# Route đăng xuất
@app.route('/logout')
@login_required # Yêu cầu đăng nhập mới được truy cập
def logout():
    logout_user() # Đăng xuất người dùng
    # Có thể thêm flash message thông báo đăng xuất thành công
    return redirect(url_for('index')) # Chuyển hướng đến trang chính

# Định nghĩa route cho trang chủ
@app.route('/')
def index():
    # Truyền trạng thái đăng nhập ra frontend
    return render_template('index.html', is_authenticated=current_user.is_authenticated)

# Khởi tạo scheduler
# Sử dụng timezone từ pytz, ví dụ múi giờ Việt Nam (Asia/Ho_Chi_Minh)
scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Ho_Chi_Minh'))
scheduler.add_job(func=fetch_vote_data, trigger="interval", minutes=10)
scheduler.start()

# Fetch data lần đầu khi ứng dụng được import (để có dữ liệu ngay từ đầu)
# fetch_vote_data() # Tạm thời bỏ comment này để tránh lỗi khi database chưa sẵn sàng trong quá trình deploy

if __name__ == '__main__':
    try:
        # Khi chạy local, có thể chạy app.run() ở đây
        # Khi deploy với Gunicorn, khối này sẽ không chạy
        # Ensure tables are created before running locally
        Base.metadata.create_all(engine) # Tạo bảng nếu chạy local
        app.run(debug=True)
        logger.info("Flask app is running locally...")
    except Exception as e:
        logger.error(f"Lỗi khởi động ứng dụng (trong __main__): {str(e)}") 
