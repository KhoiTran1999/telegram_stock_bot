```markdown
# 📈 Telegram Stock Alert Bot

Một chatbot Telegram giúp **theo dõi và cảnh báo biến động chứng khoán Việt Nam** theo thời gian thực, đồng thời tự động **gửi báo cáo phân tích danh mục bằng AI (MiniMax M2 qua OpenRouter)** vào lúc 09:00am chủ nhật hằng tuần .

> Dự án này được viết hoàn toàn bằng **Python (async)**, dùng `python-telegram-bot`, `vnstock3`, và `Flask` để hoạt động ổn định trên Render hoặc server cá nhân.

---

## 🚀 Tính năng nổi bật

- **Theo dõi realtime** giá cổ phiếu VN (nguồn: `vnstock3` – VCI)
- **Cảnh báo tự động** khi biến động mạnh (±2%, ±4%, ±6.9%)
- **Thông báo giờ mở / đóng phiên** chứng khoán Việt Nam
- **Tự động tổng hợp danh mục & phân tích bằng AI lúc 09:00am chủ nhật hằng tuần** (qua OpenRouter MiniMax M2)
- **Lưu trạng thái & danh mục user trong PostgreSQL** (bền vững qua restart / deploy)
- **Chế độ bảo trì (on/off)** lưu trong DB — không bị mất sau khi push code
- **Giao diện vui nhộn**, có câu thoại cà khịa khi giá tăng hoặc giảm 😏
- **Tự động bật lại sau 2 phút nếu bot bị tắt** (đảm bảo uptime liên tục)
- **Flask keepalive** để Render không kill process

---

## 🧩 Cấu trúc thư mục

```

telegram_stock_bot/
├── alert_bot.py        # Mã chính của bot (logic + Telegram handlers)
├── db_utils.py         # Quản lý kết nối & thao tác PostgreSQL
├── requirements.txt    # Danh sách thư viện cần cài

````

---

## ⚙️ Cài đặt

### 1. Clone repo

```bash
git clone https://github.com/<your-username>/telegram_stock_bot.git
cd telegram_stock_bot
````

### 2. Cài thư viện

```bash
pip install -r requirements.txt
```

### 3. Thiết lập biến môi trường

Tạo file `.env` (hoặc cấu hình trong Render) với nội dung:

```bash
TELEGRAM_TOKEN=<token_bot_của_bạn>
ADMIN_ID=<chat_id_admin>
OPENROUTER_API_KEY=<api_key_openrouter>   # (tùy chọn, nếu muốn dùng /report và báo cáo 16:00)
DATABASE_URL=postgresql://user:password@host:port/dbname
WEBHOOK_URL=https://<tên-miền-hoặc-render-app>/webhook
PORT=10000
```

> 🔹 `DATABASE_URL` dùng chuẩn `psycopg`
> 🔹 `WEBHOOK_URL` có thể để trống — bot sẽ tự lấy từ `RENDER_EXTERNAL_HOSTNAME` nếu chạy trên Render.

---

## 🧠 Các lệnh chính trong Telegram

| Lệnh                   | Mô tả                                           |
| ---------------------- | ----------------------------------------------- |
| `/start`               | Giới thiệu bot và hướng dẫn cơ bản              |
| `/add <MÃ>`            | Thêm mã cổ phiếu theo dõi (VD: `/add HPG`)      |
| `/remove <MÃ>`         | Xóa mã khỏi danh sách                           |
| `/list`                | Xem danh sách đang theo dõi                     |
| `/report`              | Gọi AI tạo báo cáo danh mục ngay lập tức        |
| `/on`                  | Bật bot (admin)                                 |
| `/off`                 | Tắt bot (admin)                                 |
| `/status`              | Kiểm tra trạng thái hiện tại (admin)            |
| `/announce <nội dung>` | Gửi thông báo đến tất cả user (admin)           |
| `/allwatch`            | Xem toàn bộ danh sách theo dõi của user (admin) |

---

## ☁️ Deploy trên Render

1. Tạo **Web Service** mới
2. Kéo repo này từ GitHub vào Render
3. Chọn **Start Command**:

```bash
python alert_bot.py
```

4. Thêm các **Environment Variables** như ở phần trên
5. Deploy xong, Render sẽ tự gọi `alert_bot.py` và chạy liên tục 🎯

---

## 🧠 Ghi chú thêm

* Bot hoạt động trong **giờ giao dịch Việt Nam**:
  ⏰ 09:15–11:30 & 13:00–14:45 (T2–T6)

* Ngoài giờ giao dịch, bot **tự động ngủ** cho đến phiên kế tiếp để tiết kiệm tài nguyên.

* Báo cáo AI dùng model:
  `minimax/minimax-m2:free` qua API OpenRouter
  (bạn có thể đổi model khác trong `call_chatgpt_for_report()`)

---

## 📦 Yêu cầu hệ thống

* Python ≥ 3.10
* PostgreSQL ≥ 12
* Các thư viện trong `requirements.txt`:

```
vnstock3
pandas
python-telegram-bot==20.7
pytz
requests
flask
hypercorn
psycopg[binary]
psutil
```

---

## 🧑‍💻 Tác giả

**Khôi Trần**
📍 Ho Chi Minh City
💼 Tập trung nghiên cứu thị trường chứng khoán Việt Nam và ứng dụng AI vào tài chính.

---

## 📜 Giấy phép

MIT License © 2025 Khôi Trần
