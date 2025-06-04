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
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logger.error("DATABASE_URL is not set.")
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
    real_percent = Column(Float)  # Lưu giá trị gốc không làm tròn
    board = Column(String, index=True)  # Thêm trường board để phân biệt bảng

    def __repr__(self):
        return f"<VoteRecord(timestamp='{self.timestamp}', name='{self.candidate_name}', percent={self.percent}, real_percent={self.real_percent}, board='{self.board}')>"

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
    logger.info("Bắt đầu fetch dữ liệu từ yvoting-service API (3 bảng)")
    session = Session()
    try:
        boards = [
            ("A", "https://yvoting-service.onfan.vn/api/v1/nominations?awardId=a825cb3e-0ef5-4ad5-b4cf-2cb662066701"),
            ("B", "https://yvoting-service.onfan.vn/api/v1/nominations?awardId=5de87df4-221b-4a99-9f4b-d4b3cc48f2e5"),
            ("C", "https://yvoting-service.onfan.vn/api/v1/nominations?awardId=08b7d1e0-665f-4452-8460-5a101e61f87a")
        ]
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        current_time = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        new_vote_records = []
        for board_name, url in boards:
            try:
                response = requests.get(url, headers=headers, timeout=20)
                response.raise_for_status()
                data = response.json()
                for item in data.get("data", []):
                    try:
                        name = item["character"]["name"]
                        percent = round(item["ratioVotes"], 2)
                        real_percent = item["ratioVotes"]
                        new_vote_records.append(VoteRecord(
                            timestamp=current_time,
                            candidate_name=name,
                            percent=percent,
                            real_percent=real_percent,
                            board=board_name
                        ))
                    except KeyError as e:
                        logger.error(f"Missing key in API response item: {e}. Item: {item}")
                        continue
                    except Exception as e:
                        logger.error(f"Lỗi khi xử lý item từ API: {e}. Item: {item}")
                        continue
            except Exception as e:
                logger.error(f"Lỗi khi fetch dữ liệu bảng {board_name}: {e}")
                continue
        if new_vote_records:
            session.add_all(new_vote_records)
            session.commit()
            logger.info(f"Đã lưu {len(new_vote_records)} bản ghi vote mới vào database tại {current_time}")
        else:
            logger.warning("API returned no data or failed to parse.")
    except Exception as e:
        logger.error(f"Lỗi không xác định khi fetch data: {e}")
        session.rollback()
        raise
    finally:
        session.close()

# API trả về dữ liệu vote hiện tại
@app.route('/api/vote-data')
def get_vote_data():
    session = Session()
    try:
        boards = ["A", "B", "C"]
        result = {}
        for board in boards:
            latest_timestamp = session.query(VoteRecord.timestamp).filter_by(board=board).order_by(desc(VoteRecord.timestamp)).limit(1).scalar()
            if not latest_timestamp:
                result[board] = {'last_update': None, 'candidates': []}
                continue
            latest_records = session.query(VoteRecord).filter_by(timestamp=latest_timestamp, board=board).all()
            candidates = [{
                'name': record.candidate_name,
                'percent': record.percent
            } for record in latest_records]
            last_update_str = latest_timestamp.strftime("%Y-%m-%d %H:%M:%S")
            result[board] = {'last_update': last_update_str, 'candidates': candidates}
        return jsonify(result)
    except Exception as e:
        logger.error(f"Lỗi khi lấy dữ liệu vote hiện tại từ DB: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

# API trả về lịch sử
@app.route('/api/history')
def get_history():
    session = Session()
    try:
        interval = request.args.get('interval', '1d')
        now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
        interval_map = {
            '10m': 10 * 60,
            '30m': 30 * 60,
            '1h': 60 * 60,
            '5h': 5 * 60 * 60,
            '1d': 24 * 60 * 60,
            '3d': 3 * 24 * 60 * 60,
            '7d': 7 * 24 * 60 * 60
        }
        step_map = {
            '10m': 10,
            '30m': 30,
            '1h': 60,
            '5h': 300
        }
        seconds = interval_map.get(interval, 24 * 60 * 60)
        step_minutes = step_map.get(interval, None)
        time_threshold = now - timedelta(seconds=seconds)
        query = session.query(VoteRecord).filter(VoteRecord.timestamp >= time_threshold)
        query = query.order_by(desc(VoteRecord.timestamp))
        records = query.order_by(desc(VoteRecord.timestamp), VoteRecord.candidate_name).all()
        # Lọc theo step
        filtered_snapshots = []
        last_snapshot_time = None
        for record in records:
            ts = record.timestamp
            # Luôn convert về Asia/Ho_Chi_Minh (GMT+7)
            if ts.tzinfo is None:
                ts = pytz.utc.localize(ts).astimezone(pytz.timezone('Asia/Ho_Chi_Minh'))
            else:
                ts = ts.astimezone(pytz.timezone('Asia/Ho_Chi_Minh'))
            if not last_snapshot_time or (step_minutes and (last_snapshot_time - ts).total_seconds() >= step_minutes * 60) or not step_minutes:
                snapshot_time = ts.replace(second=0, microsecond=0)
                if not any(abs((snapshot_time - s['timestamp']).total_seconds()) < 60 for s in filtered_snapshots):
                    filtered_snapshots.append({'timestamp': snapshot_time, 'candidates': []})
                    last_snapshot_time = snapshot_time
        # Gán candidates cho từng snapshot
        for snap in filtered_snapshots:
            snap['candidates'] = [
                {'name': r.candidate_name, 'percent': r.percent, 'real_percent': r.real_percent}
                for r in records 
                if (r.timestamp.tzinfo is None and pytz.utc.localize(r.timestamp).astimezone(pytz.timezone('Asia/Ho_Chi_Minh')).replace(second=0, microsecond=0) == snap['timestamp'])
                or (r.timestamp.tzinfo is not None and r.timestamp.astimezone(pytz.timezone('Asia/Ho_Chi_Minh')).replace(second=0, microsecond=0) == snap['timestamp'])
            ]
        # Sắp xếp lại cho mốc mới nhất lên trên
        filtered_snapshots.sort(key=lambda x: x['timestamp'], reverse=True)
        # Định dạng timestamp trả về
        for snap in filtered_snapshots:
            snap['timestamp'] = snap['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify(filtered_snapshots)
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

def should_fetch_data():
    now = datetime.now(pytz.timezone('Asia/Ho_Chi_Minh'))
    start_time = datetime(2024, 6, 4, 21, 30, tzinfo=pytz.timezone('Asia/Ho_Chi_Minh'))
    end_time = datetime(2024, 6, 4, 23, 5, tzinfo=pytz.timezone('Asia/Ho_Chi_Minh'))
    
    # Nếu đã qua 23h05 ngày 4/6 thì dừng cập nhật
    if now > end_time:
        return False
        
    # Nếu trong khoảng 21h30-23h05 ngày 4/6 thì cập nhật mỗi 1 phút
    if start_time <= now <= end_time:
        return True
        
    # Các trường hợp còn lại cập nhật mỗi 5 phút
    return True

def scheduled_fetch():
    if should_fetch_data():
        fetch_vote_data()

# Thêm job cập nhật mỗi 5 phút
scheduler.add_job(func=scheduled_fetch, trigger="interval", minutes=5)

# Thêm job cập nhật mỗi 1 phút trong khoảng thời gian đặc biệt
scheduler.add_job(
    func=scheduled_fetch,
    trigger="interval",
    minutes=1,
    start_date=datetime(2024, 6, 4, 21, 30, tzinfo=pytz.timezone('Asia/Ho_Chi_Minh')),
    end_date=datetime(2024, 6, 4, 23, 5, tzinfo=pytz.timezone('Asia/Ho_Chi_Minh'))
)

scheduler.start()

# Fetch data lần đầu khi ứng dụng được import
fetch_vote_data()

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
