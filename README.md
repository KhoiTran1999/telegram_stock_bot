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
- **Screener (Bộ lọc)**: Lọc cổ phiếu theo tiêu chí định giá (Rẻ/Đắt) dựa trên P/E, P/B lịch sử (Mean Reversion). Hỗ trợ lọc theo 19 nhóm ngành chi tiết (Ngân hàng, Bất động sản, Bán lẻ, CNTT...).
- **Hiệu suất ngành**: Tab riêng trong WebApp Screener hiển thị biểu đồ Plotly + bảng so sánh % biến động 12 tuần và 6 tháng của từng ngành (bao gồm VNINDEX). Cho phép đánh giá nhanh dòng tiền luân chuyển giữa các nhóm.

### 🤖 AI & Tự động hóa (Powered by Gemini)
- **Bản tin sáng (Morning Digest)**: Tự động tổng hợp tin tức vĩ mô & doanh nghiệp, dùng AI để tóm tắt và đánh giá tác động (7:00 AM).
- **Tổng kết cuối ngày (EOD Summary)**: AI nhận định thị trường, dòng tiền và tâm lý đám đông sau giờ giao dịch (15:00).
- **Báo cáo danh mục `/report`**:
    - Giao diện WebApp mới với tiến trình realtime (10% → 70%) giúp Pro user biết trạng thái AI Analyst.
    - Gateway kiểm tra cache theo danh mục chuẩn hoá; nếu báo cáo còn hạn sẽ mở ngay, nếu không sẽ đẩy task `GEN_REPORT` sang Worker qua Redis.
    - Worker tái sử dụng `report_cache`, giảm lượt gọi Gemini cho báo cáo trùng và cho phép Weekly Batch tái phát lại báo cáo cũ nếu chưa cần cập nhật.
    - Free user vẫn có thể mở bản báo cáo gần nhất ở chế độ chỉ đọc (không phát sinh lượt AI mới).

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
- **Frontend**: AlpineJS (Screener, Admin Dashboard), Plotly.js (Charts)

### Cấu trúc thư mục chính

```
telegram_stock_bot/
├── gateway.py              # [MAIN] Xử lý Webhook, lệnh Telegram, API Server
├── worker.py               # [BACKGROUND] Xử lý tác vụ nặng: Quét giá, AI, Gửi báo cáo
├── db_utils.py             # Thao tác Database (PostgreSQL)
├── chart_utils.py          # Vẽ biểu đồ (Matplotlib/Mplfinance)
├── digest_template.py      # HTML Templates cho Web App (Dashboard, Report, Screener)
├── news_seen_cache.py      # Quản lý cache tin tức đã gửi
├── profile_cache.py        # Cache thông tin hồ sơ doanh nghiệp
├── report_cache.py         # Cache báo cáo phân tích
├── redis_client.py         # Cấu hình kết nối Redis
├── sectors.json            # Mapping mã cổ phiếu -> Ngành
├── update_db.py            # Script migration database
└── requirements.txt        # Các thư viện phụ thuộc
```

### Luồng hoạt động (Gateway - Worker Pattern)

1.  **Gateway (`gateway.py`)**:
    *   Nhận tin nhắn/lệnh từ Telegram (Webhook).
    *   Xử lý các phản hồi nhanh (Menu, Setting, Add/Remove mã).
    *   Đẩy các tác vụ nặng (Tạo báo cáo AI, Lọc cổ phiếu) vào hàng đợi **Redis**.
    *   Phục vụ các trang Web App (Dashboard, Chart View, Screener).

2.  **Worker (`worker.py`)**:
    *   Lắng nghe hàng đợi từ Redis.
    *   **Loops**:
        *   `alert_loop`: Quét giá cổ phiếu liên tục, bắn cảnh báo nếu biến động.
        *   `market_monitor_fetcher_loop`: Quét giá VN30F1M, VNINDEX, VN30 (Unified).
        *   `market_monitor_alert_loop`: Xử lý logic cảnh báo thị trường chung.
        *   `job_daily_digest`: Tạo bản tin sáng lúc 7:00.
        *   `job_scan_news`: Quét tin tức RSS định kỳ.
        *   `job_nightly_valuation`: Tính toán P/E, P/B trung bình 5 năm hàng đêm.
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
GEMINI_API_KEY_3=<backup-key-2>

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
| `/agent <macro|biz|tech|all>` | (Admin only) Trigger thủ công bộ 3 agent (Vĩ mô, Doanh nghiệp, Kỹ thuật) để thu thập dữ liệu và log kết quả. |
| `/agentlog <macro|biz|tech|all>` | (Admin only) Đọc nhanh dữ liệu agent/bundle đang lưu trên Redis và log ra chat. |

---

### 🧠 Multi-Agent Manual Trigger

- Lệnh `/agent` chỉ dành cho Admin, cho phép chạy từng agent hoặc toàn bộ pipeline.
- Worker lưu kết quả từng agent vào Redis dưới dạng `agent:<type>:current` (TTL 24h) và bundle tổng hợp tại `agent:bundle:<chat_id>:current` (TTL 7 ngày).
- Sau khi xử lý xong, Worker tự động gửi báo cáo Markdown gồm request ID, scope và trạng thái của từng agent để admin dễ dàng tinh chỉnh prompt.
- Sử dụng `/agentlog <type>` để đọc lại cache hiện tại (macro, biz, tech hoặc all) mà không cần rerun agent.

---

## 🛠️ Cơ chế AI & Định giá

### 1. Mean Reversion Screener
Hệ thống tự động tính toán P/E và P/B trung bình 5 năm của cổ phiếu.
- **Rẻ**: Giá hiện tại thấp hơn trung bình lịch sử (>10%).
- **Đắt**: Giá hiện tại cao hơn trung bình lịch sử.
- **Phân ngành**: Hỗ trợ lọc theo 19 nhóm ngành (Ngân hàng, BĐS, Thép, Bán lẻ, Hóa chất...) dựa trên dữ liệu từ `sectors.json`.
Dữ liệu này được tính toán hàng đêm (`job_nightly_valuation`) và lưu vào Redis để truy xuất nhanh.

### 2. Hiệu suất ngành (Sector Performance)
- **Nguồn dữ liệu**: Sử dụng cùng payload định giá (Redis `historical_valuation`) nhưng tổng hợp theo ngành. Chỉ các mã đạt điều kiện thanh khoản ≥ 50 tỷ và vốn hóa ≥ 5.000 tỷ mới tham gia tính toán, đồng thời loại bỏ một số mã đặc biệt (VIC/VHM/VRE) để tránh bóp méo số liệu.
- **Cách tính**:
    - `change_12w`: % biến động giá dựa trên giá đóng cửa hiện tại so với giá cách đây ~84 ngày (12 tuần, dữ liệu daily).
    - `change_6m`: % biến động giá so với mốc ~180 ngày trước.
    - Mỗi ngành lấy trung bình cộng giản đơn của các mã trong ngành có dữ liệu hợp lệ (mỗi mã 1 phiếu). VNINDEX được thêm như một “ngành tham chiếu”.
- **Hiển thị**: WebApp Screener tab “Hiệu suất Ngành” gồm biểu đồ thanh Plotly và bảng so sánh 12W/6M đã được sort giảm dần theo 6M (fallback 12W khi thiếu dữ liệu). Dữ liệu làm mới 1 lần/đêm cùng job định giá.

### 2. AI News Summary
- Worker quét tin từ các nguồn RSS (CafeF, Vietstock, VnEconomy).
- Gemini AI lọc tin rác, phân loại (Vĩ mô/Doanh nghiệp) và chấm điểm tác động.
- Chỉ những tin quan trọng mới được đưa vào bản tin sáng.

---

## 🎨 Giao diện (UI/UX)
- **Dark Mode**: Hỗ trợ giao diện tối tự động theo cài đặt Telegram.
- **Responsive**: Tối ưu hiển thị trên Mobile.
- **Interactive Charts**: Biểu đồ tương tác (Zoom, Pan) sử dụng Plotly.js.

---

## 🤝 Đóng góp

Dự án được phát triển cá nhân. Mọi đóng góp hoặc báo lỗi vui lòng liên hệ qua Telegram Admin.

---

## 📝 License

Proprietary Software.
