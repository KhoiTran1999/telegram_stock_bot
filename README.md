# 📈 Telegram Stock Bot - Trợ Lý Chứng Khoán AI

Bot Telegram thông minh hỗ trợ nhà đầu tư chứng khoán Việt Nam với khả năng theo dõi giá realtime, cảnh báo tín hiệu, và phân tích thị trường tự động bằng AI (Gemini).

![Chatbot Preview](chatbotImage.png)

---

## 🎯 Tính năng nổi bật

### 📊 Dữ liệu & Thị trường
- **Realtime Tracking**: Cập nhật giá cổ phiếu và chỉ số VN30F1M theo thời gian thực.
- **Cảnh báo thông minh**:
  - **Stock Alert**: Báo ngay khi giá biến động mạnh (≥ 2%).
  - **Market Monitor (Unified)**: Theo dõi sát sao 3 chỉ số quan trọng:
    - **VN30F1M (Phái sinh)**: Cảnh báo biến động ±5 điểm.
    - **VNINDEX**: Cảnh báo biến động ±5 điểm.
    - **VN30**: Cảnh báo biến động ±5 điểm.
- **Biểu đồ kỹ thuật**: Vẽ chart nến, RSI, Volume ngay trong Telegram (Mini chart & Full chart).
- **Screener (Bộ lọc)**: Lọc cổ phiếu theo tiêu chí định giá (Rẻ/Đắt) dựa trên P/E, P/B lịch sử (Mean Reversion).

### 🤖 AI & Tự động hóa (Powered by Gemini)
- **Bản tin sáng (Morning Digest)**: Tự động tổng hợp tin tức vĩ mô & doanh nghiệp, dùng AI để tóm tắt và đánh giá tác động (7:00 AM).
- **Tổng kết cuối ngày (EOD Summary)**: AI nhận định thị trường, dòng tiền và tâm lý đám đông sau giờ giao dịch (15:00).
- **Phân tích danh mục**: Đánh giá sức khỏe danh mục đầu tư, so sánh định giá hiện tại với lịch sử 5 năm.

### 👤 Quản lý người dùng
- **Phân cấp tài khoản**:
  - **Free**: Theo dõi 1 mã, tính năng cơ bản.
  - **Pro**: Không giới hạn mã, nhận báo cáo AI, cảnh báo phái sinh, lọc cổ phiếu.
  - **Trial**: Dùng thử Full tính năng Pro trong 10 ngày (`/trial`).
- **Admin Dashboard**: Công cụ quản lý người dùng, gửi thông báo (Broadcast), xem thống kê.

---

## 🏗️ Kiến trúc hệ thống

Dự án sử dụng mô hình **Gateway - Worker** để đảm bảo hiệu năng và khả năng mở rộng.

```mermaid
graph TD
    User["User Telegram"] -->|Message/Command| Gateway["Gateway (Flask/Hypercorn)"]
    Gateway -->|Push Task| Redis[("Redis Queue & Cache")]
    Gateway -->|Read/Write| DB[("PostgreSQL")]
    Gateway -->|Reply| User

    Worker["Worker Process"] -->|Pop Task| Redis
    Worker -->|Pub Result| Redis
    Worker -->|Read/Write| DB
    
    Worker -->|Fetch Data| Data["Data Sources (Vnstock, RSS)"]
    Worker -->|Analyze| AI["Gemini AI"]
    
    Redis -->|Sub Result| Gateway
```

### Tech Stack
- **Language**: Python 3.12+
- **Framework**: 
  - `python-telegram-bot` (Bot Interface)
  - `Flask` + `Hypercorn` (Web Server & Webhook)
- **Database**: PostgreSQL (Lưu user, watchlist, logs)
- **Cache & Message Queue**: Redis (Lưu trạng thái, cache giá, giao tiếp giữa Gateway và Worker)
- **AI Model**: Google Gemini 2.5 Flash/Pro
- **Data Source**: `vnstock`, `feedparser` (RSS CafeF, Vietstock...)

### Cấu trúc thư mục chính

```
telegram_stock_bot/
├── gateway.py              # [MAIN] Xử lý Webhook, lệnh Telegram, API Server
├── worker.py               # [BACKGROUND] Xử lý tác vụ nặng: Quét giá, AI, Gửi báo cáo
├── db_utils.py             # Thao tác Database (PostgreSQL)
├── chart_utils.py          # Vẽ biểu đồ (Matplotlib/Mplfinance)
├── digest_template.py      # HTML Templates cho Web App (Dashboard, Report)
├── news_seen_cache.py      # Quản lý cache tin tức đã gửi
├── profile_cache.py        # Cache thông tin hồ sơ doanh nghiệp
├── report_cache.py         # Cache báo cáo phân tích
├── redis_client.py         # Cấu hình kết nối Redis
├── update_db.py            # Script migration database
└── requirements.txt        # Các thư viện phụ thuộc
```

### Luồng hoạt động (Gateway - Worker Pattern)

1.  **Gateway (`gateway.py`)**:
    *   Nhận tin nhắn/lệnh từ Telegram (Webhook).
    *   Xử lý các phản hồi nhanh (Menu, Setting, Add/Remove mã).
    *   Đẩy các tác vụ nặng (Tạo báo cáo AI, Lọc cổ phiếu) vào hàng đợi **Redis**.
    *   Phục vụ các trang Web App (Dashboard, Chart View).

2.  **Worker (`worker.py`)**:
    *   Lắng nghe hàng đợi từ Redis.
    *   **Loops**:
        *   `alert_loop`: Quét giá cổ phiếu liên tục, bắn cảnh báo nếu biến động.
        *   `market_monitor_fetcher_loop`: Quét giá VN30F1M, VNINDEX, VN30 (Unified).
        *   `market_monitor_alert_loop`: Xử lý logic cảnh báo thị trường chung.
        *   `job_daily_digest`: Tạo bản tin sáng lúc 7:00.
        *   `job_scan_news`: Quét tin tức RSS định kỳ.
    *   Xử lý AI: Gọi Gemini API để phân tích và trả kết quả về cho Gateway (qua Redis Pub/Sub).

---

## 🚀 Cài đặt & Triển khai

### Yêu cầu
- Python 3.12
- PostgreSQL
- Redis

### Biến môi trường (.env)

Tạo file `.env` với các thông tin sau:

```env
# Telegram
TELEGRAM_TOKEN=<your-bot-token>
ADMIN_ID=<your-telegram-id>

# Database & Redis
DATABASE_URL=postgresql://user:pass@host:port/dbname
REDIS_URL=redis://host:port/0

# AI (Google Gemini)
GEMINI_API_KEY=<your-api-key>
GEMINI_API_KEY_2=<backup-key>

# Server
PASSENGER_PORT=10000
RENDER_EXTERNAL_URL=<your-app-url>

# Payment (Optional - SePay)
SEPAY_TOKEN=<token>
SEPAY_QR_BANK=<bank-code>
SEPAY_QR_ACC=<account-number>
```

### Chạy Local

1.  **Cài đặt dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Khởi tạo Database**:
    ```bash
    python update_db.py
    ```

3.  **Chạy Worker** (Mở terminal 1):
    ```bash
    python worker.py
    ```

4.  **Chạy Gateway** (Mở terminal 2):
    ```bash
    python gateway.py
    ```

---

## 🎮 Danh sách lệnh (Commands)

| Lệnh | Mô tả |
| :--- | :--- |
| `/start` | Mở Dashboard chính, menu quản lý. |
| `/help` | Xem hướng dẫn sử dụng. |
| `/add <MÃ>` | Thêm mã vào danh sách theo dõi (VD: `/add HPG`). |
| `/remove <MÃ>` | Xóa mã khỏi danh sách. |
| `/list` | Xem danh sách mã đang theo dõi. |
| `/setting` | Cài đặt tài khoản, bật/tắt cảnh báo. |
| `/trial` | Kích hoạt dùng thử gói Pro 10 ngày. |
| `/admin` | (Admin only) Mở trang quản trị. |
| `/announce <msg>` | (Admin only) Gửi thông báo tới tất cả user. |

---

## 🛠️ Cơ chế AI & Định giá

### 1. Mean Reversion Screener
Hệ thống tự động tính toán P/E và P/B trung bình 5 năm của cổ phiếu.
- **Rẻ**: Giá hiện tại thấp hơn trung bình lịch sử (>10%).
- **Đắt**: Giá hiện tại cao hơn trung bình lịch sử.
Dữ liệu này được tính toán hàng đêm (`job_nightly_valuation`) và lưu vào Redis để truy xuất nhanh.

### 2. AI News Summary
- Worker quét tin từ các nguồn RSS (CafeF, Vietstock, VnEconomy).
- Gemini AI lọc tin rác, phân loại (Vĩ mô/Doanh nghiệp) và chấm điểm tác động.
- Chỉ những tin quan trọng mới được đưa vào bản tin sáng.

---

## 🤝 Đóng góp

Dự án được phát triển cá nhân. Mọi đóng góp hoặc báo lỗi vui lòng liên hệ qua Telegram Admin.

---

## 📝 License

Proprietary Software.
