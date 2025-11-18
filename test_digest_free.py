# test_digest_free.py
import json
import uuid
import os
import datetime
import redis
from flask import Flask, render_template_string

# --- CẤU HÌNH ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PORT = 5001

app = Flask(__name__)

# Kết nối Redis
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
except Exception:
    print("⚠️ CẢNH BÁO: Không kết nối được Redis! Web App sẽ lỗi.")
    r = None

# --- TEMPLATE HTML (BẢN UPSELL ĐẦY ĐỦ TÍNH NĂNG) ---
DIGEST_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>StockBot Digest</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --button-color: var(--tg-theme-button-color, #007aff);
            --button-text-color: var(--tg-theme-button-text-color, #fff);
            --surface-color: var(--tg-theme-secondary-bg-color, #ffffff);
            
            /* Fintech Colors */
            --color-success: #34c759; --color-success-bg: rgba(52, 199, 89, 0.1);
            --color-info: #007aff;    --color-info-bg: rgba(0, 122, 255, 0.1);
            --color-warn: #ff9500;    --color-warn-bg: rgba(255, 149, 0, 0.1);
            --color-purple: #af52de;  --color-purple-bg: rgba(175, 82, 222, 0.1);
            --color-indigo: #5856d6;  --color-indigo-bg: rgba(88, 86, 214, 0.1);
            
            --border-radius: 16px;
            --shadow-sm: 0 2px 8px rgba(0,0,0,0.04);
            --brand-gradient: linear-gradient(135deg, #007aff 0%, #af52de 100%);
        }

        @media (prefers-color-scheme: dark) { :root { --shadow-sm: 0 2px 8px rgba(0,0,0,0.2); } }

        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-color); margin: 0; padding: 20px 16px 40px 16px; font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased; }
        
        .header { text-align: center; margin-bottom: 32px; animation: fadeInDown 0.6s cubic-bezier(0.2, 0.8, 0.2, 1); }
        .date-badge { display: inline-flex; align-items: center; gap: 6px; background-color: var(--surface-color); padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--hint-color); box-shadow: 0 2px 6px rgba(0,0,0,0.03); margin-bottom: 12px; border: 1px solid rgba(0,0,0,0.05); }
        .header-title { font-size: 32px; font-weight: 800; margin: 0; letter-spacing: -1px; line-height: 1.2; background: var(--brand-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; color: var(--button-color); }
        .header-desc { font-size: 14px; color: var(--hint-color); margin-top: 8px; font-weight: 500; }

        /* Card Design */
        .section-card { background-color: var(--surface-color); border-radius: var(--border-radius); margin-bottom: 20px; box-shadow: var(--shadow-sm); overflow: hidden; animation: fadeInUp 0.5s ease; animation-fill-mode: both; border: 1px solid rgba(0,0,0,0.02); }
        .card-header { padding: 16px 16px 10px 16px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .card-icon { width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; }
        .card-title { font-size: 17px; font-weight: 700; color: var(--text-color); letter-spacing: -0.3px; }

        .list-item { padding: 14px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); display: block; text-decoration: none; color: inherit; transition: background-color 0.2s; }
        .list-item:last-child { border-bottom: none; }
        .list-item:active { background-color: rgba(0,0,0,0.05); }
        .list-item.hidden { display: none; }
        
        .item-title { font-size: 15px; font-weight: 500; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .load-more-container { padding: 12px; text-align: center; border-top: 1px solid rgba(0,0,0,0.05); }
        .load-more-btn { background: none; border: none; color: var(--button-color); font-size: 13px; font-weight: 600; cursor: pointer; padding: 8px 16px; border-radius: 20px; background-color: rgba(0,122,255,0.05); }
        .load-more-container.hidden { display: none; }

        /* Theme Colors */
        .theme-orange .card-icon { background: var(--color-warn-bg); color: var(--color-warn); }
        .theme-purple .card-icon { background: var(--color-purple-bg); color: var(--color-purple); }

        /* --- PREMIUM UPSELL CARD (Full Feature) --- */
        .premium-card {
            background: var(--brand-gradient);
            border-radius: 24px;
            padding: 24px;
            color: white;
            text-align: center;
            margin-top: 32px;
            box-shadow: 0 10px 30px rgba(0, 122, 255, 0.3);
            position: relative;
            overflow: hidden;
        }
        
        .premium-card::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle, rgba(255,255,255,0.15) 0%, transparent 70%);
            transform: rotate(30deg);
            pointer-events: none;
        }

        .premium-header {
            font-size: 19px;
            font-weight: 800;
            margin-bottom: 20px;
            letter-spacing: -0.5px;
            line-height: 1.3;
        }

        .premium-features {
            text-align: left;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 16px;
            padding: 16px 16px;
            margin-bottom: 20px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.2);
        }

        .p-feature {
            display: flex;
            align-items: flex-start;
            gap: 10px;
            margin-bottom: 12px;
            font-size: 13px;
            line-height: 1.4;
        }
        .p-feature:last-child { margin-bottom: 0; }
        .p-icon { font-size: 16px; min-width: 20px; }
        .p-text b { font-weight: 700; display: block; font-size: 14px; margin-bottom: 1px; }
        .p-text { opacity: 0.95; }

        .premium-btn {
            display: block;
            width: 100%;
            padding: 15px;
            background-color: #fff;
            color: #007aff;
            border: none;
            border-radius: 14px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.1s;
            box-shadow: 0 6px 15px rgba(0,0,0,0.15);
        }
        .premium-btn:active { transform: scale(0.97); opacity: 0.95; }
        .premium-note { font-size: 12px; margin-top: 14px; opacity: 0.85; font-weight: 500; }

        .close-btn {
            display: block;
            width: 100%;
            padding: 14px;
            background-color: var(--surface-color);
            color: var(--text-color);
            border: 1px solid rgba(0,0,0,0.05);
            border-radius: 12px;
            font-size: 15px;
            font-weight: 600;
            margin-top: 20px;
            cursor: pointer;
        }

        @keyframes fadeInUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes fadeInDown { from { opacity: 0; transform: translateY(-20px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="header">
        <div class="date-badge"><span>🗓️</span> {{ date_str }}</div>
        <h1 class="header-title">Daily Digest</h1>
        <div class="header-desc">Tổng hợp thị trường & Danh mục của bạn</div>
    </div>

    {% if data.specialized %}
    <div class="section-card theme-orange" id="section-specialized">
        <div class="card-header"><div class="card-icon">🏢</div><div class="card-title">Tin Doanh Nghiệp ({{ data.specialized|length }})</div></div>
        <div class="list-container">
            {% for item in data.specialized %}
            <a href="{{ item.link }}" target="_blank" class="list-item">
                <div class="item-title">{{ item.title }}</div>
            </a>
            {% endfor %}
        </div>
        <div class="load-more-container hidden"><button class="load-more-btn" data-state="expand">Xem thêm ↓</button></div>
    </div>
    {% endif %}

    {% if data.macro %}
    <div class="section-card theme-purple" id="section-macro">
        <div class="card-header"><div class="card-icon">🌍</div><div class="card-title">Vĩ Mô & Chính Sách ({{ data.macro|length }})</div></div>
        <div class="list-container">
            {% for item in data.macro %}
            <a href="{{ item.link }}" target="_blank" class="list-item">
                <div class="item-title">{{ item.title }}</div>
            </a>
            {% endfor %}
        </div>
        <div class="load-more-container hidden"><button class="load-more-btn" data-state="expand">Xem thêm ↓</button></div>
    </div>
    {% endif %}

    {% if not data.is_pro %}
    <div class="premium-card">
        <div class="premium-header">Mở khóa 5 công cụ mạnh mẽ<br>của StockBot Pro 🚀</div>
        
        <div class="premium-features">
            <div class="p-feature">
                <span class="p-icon">💎</span> 
                <span class="p-text"><b>Value Screener</b>Lọc cổ phiếu định giá rẻ (P/E, P/B) mỗi ngày</span>
            </div>
            <div class="p-feature">
                <span class="p-icon">🤖</span> 
                <span class="p-text"><b>Weekly AI Report</b>Phân tích & nhận định danh mục mỗi Chủ Nhật</span>
            </div>
            <div class="p-feature">
                <span class="p-icon">ℹ️</span> 
                <span class="p-text"><b>Hồ sơ Doanh nghiệp</b>Tra cứu mô hình & vị thế ngành (lệnh /info)</span>
            </div>
            <div class="p-feature">
                <span class="p-icon">📊</span> 
                <span class="p-text"><b>Báo cáo Tài chính</b>Thông báo sớm nhất ngay khi công bố</span>
            </div>
            <div class="p-feature">
                <span class="p-icon">📈</span> 
                <span class="p-text"><b>Phái sinh VN30F1M</b>Cảnh báo realtime biến động ±5 điểm</span>
            </div>
        </div>

        <button class="premium-btn" onclick="Telegram.WebApp.close()">🔥 Gõ /upgrade để Nâng cấp ngay</button>
        <div class="premium-note">Chỉ 99k/tháng. Hỗ trợ thanh toán QR Code tự động.</div>
    </div>
    {% endif %}

    <button class="close-btn" onclick="Telegram.WebApp.close()">Đóng</button>

    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
        function initPagination(sectionId, itemsPerPage = 5) {
            const section = document.getElementById(sectionId);
            if (!section) return;
            const listContainer = section.querySelector('.list-container');
            const items = listContainer.querySelectorAll('.list-item');
            const loadMoreContainer = section.querySelector('.load-more-container');
            const loadMoreBtn = section.querySelector('.load-more-btn');
            if (items.length <= itemsPerPage) return;
            let visibleCount = itemsPerPage;
            const renderItems = () => {
                items.forEach((item, index) => {
                    if (index < visibleCount) { item.classList.remove('hidden'); item.style.animation = 'fadeInUp 0.3s ease forwards'; }
                    else { item.classList.add('hidden'); }
                });
            };
            renderItems();
            loadMoreContainer.classList.remove('hidden');
            loadMoreBtn.onclick = () => {
                const currentState = loadMoreBtn.getAttribute('data-state');
                if (currentState === 'collapse') {
                    visibleCount = itemsPerPage; renderItems(); loadMoreBtn.textContent = "Xem thêm ↓"; loadMoreBtn.setAttribute('data-state', 'expand'); section.scrollIntoView({ behavior: 'smooth', block: 'center' });
                } else {
                    visibleCount += itemsPerPage; if (visibleCount >= items.length) { visibleCount = items.length; loadMoreBtn.textContent = "Thu gọn ↑"; loadMoreBtn.setAttribute('data-state', 'collapse'); } renderItems();
                }
            };
        }
        document.addEventListener('DOMContentLoaded', () => {
            initPagination('section-specialized', 5);
            initPagination('section-macro', 5);
        });
    </script>
</body>
</html>
"""

# --- TẠO DỮ LIỆU GIẢ FREE USER ---
def generate_mock_data_free():
    return {
        "is_pro": False,    
        "value_stocks": [],
        "bctc": [],
        "reports": [],
        "specialized": [
            {"title": "Hòa Phát đạt sản lượng thép kỷ lục trong tháng 3", "link": "https://google.com"},
            {"title": "Vinamilk chốt danh sách cổ đông trả cổ tức đợt 2", "link": "https://google.com"},
            {"title": "Techcombank lãi trước thuế quý 1 tăng 20%", "link": "https://google.com"},
            {"title": "Vingroup công bố kế hoạch kinh doanh năm 2025", "link": "https://google.com"}
        ],
        "macro": [
            {"title": "Ngân hàng Nhà nước tiếp tục hút tiền qua tín phiếu", "link": "https://google.com"},
            {"title": "GDP Quý 1 tăng trưởng vượt dự báo", "link": "https://google.com"}
        ]
    }

@app.route('/')
def index():
    if not r: return "Redis Error"
    try:
        digest_id = uuid.uuid4().hex
        mock_data = generate_mock_data_free()
        r.set(f"digest_web:{digest_id}", json.dumps(mock_data), ex=3600)
        url = f"/digest/{digest_id}"
        return f"""
        <div style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <h1>🧪 Test Digest V3 (Free User Upsell Pro)</h1>
            <a href="{url}" style="display: inline-block; padding: 15px 30px; background-color: #007aff; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 18px;">👉 Xem Bản Tin Free</a>
            <p style="margin-top: 20px; color: #666;">Tip: Nhấn <b>F12</b> -> 📱 Mobile View</p>
        </div>
        """
    except Exception as e:
        return f"<h2>Lỗi: {e}</h2>"

@app.route('/digest/<digest_id>')
def view_digest(digest_id):
    if not r: return "Redis Error"
    raw = r.get(f"digest_web:{digest_id}")
    if not raw:
        return render_template_string("<h2>Hết hạn</h2>"), 404
    
    data = json.loads(raw)
    vn_tz = datetime.timezone(datetime.timedelta(hours=7))
    date_str = datetime.datetime.now(vn_tz).strftime("Ngày %d/%m/%Y")
    
    return render_template_string(DIGEST_HTML_TEMPLATE, data=data, date_str=date_str)

if __name__ == '__main__':
    print(f"🚀 Server test Free User đang chạy tại: http://localhost:{PORT}")
    app.run(port=PORT, debug=True)