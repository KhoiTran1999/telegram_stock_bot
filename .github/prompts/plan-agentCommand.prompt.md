## Feature: Command-triggered Multi-Agent Pipeline

### Goals
- Lệnh `/agent <macro|biz|tech|all>` cho admin nhằm chạy thủ công 3 agent (Vĩ mô, Doanh nghiệp, Kỹ thuật).
- Worker nhận lệnh, chạy crawler placeholder cho từng agent, gửi dữ liệu sang AI tổng hợp, lưu kết quả vào Redis.
- Trả về admin bản log đầy đủ để tinh chỉnh prompt.

### Scope & Constraints
- Trigger chỉ qua command của admin (không cron).
- Tạm thời logic crawl và AI tổng hợp là stub (để user tự bổ sung sau).
- Redis lưu dữ liệu ở dạng JSON; không cần versioning/rollback lịch sử, chỉ bản hiện tại đủ dùng.

### Redis Schema (proposed)
- `agent:macro:<date|ts>`: JSON kết quả agent Vĩ mô (`{"source":"", "insights":[], "raw_data":{}}`).
- `agent:biz:<date|ts>`: tương tự cho Doanh nghiệp.
- `agent:tech:<date|ts>`: tương tự cho Kỹ thuật.
- `agent:bundle:<chat_id>:<ts>`: gộp cả ba agent + summary AI (`{"macro":{}, "biz":{}, "tech":{}, "ai_summary":""}`).
- TTL: agent key 24h, bundle 7 ngày (dễ so sánh trong ngắn hạn).

### Gateway (`gateway.py`)
1. Thêm handler `/agent`:
   - Kiểm tra user là ADMIN; nếu không thì im lặng.
   - Parse đối số (`macro|biz|tech|all`), default `all`.
   - Đẩy payload tới worker qua `push_to_worker`:
     ```json
     {"cmd":"CMD_AGENT_RUN","chat_id":<admin_id>,"scope":"macro"}
     ```
   - Báo lại admin “Đang chạy agent macro…”.
2. (Optional) Cho phép `/agent dump` để lấy lại kết quả mới nhất từ Redis (nếu cần thảo luận thêm).

### Worker (`worker.py`)
1. Lắng nghe `CMD_AGENT_RUN`:
   - Phân nhánh theo scope (`macro|biz|tech|all`).
   - Với mỗi agent cần chạy:
     - Gọi hàm stub `await run_macro_agent()`… trả về dict placeholder.
     - Lưu vào Redis key tương ứng + TTL.
   - Gom kết quả thành bundle + stub AI summary (`"TODO: tổng hợp"`).
   - Lưu bundle key + TTL.
2. Push thông báo về Gateway cho admin:
   - Nội dung: tóm tắt 3 agent + link/log.
   - Có thể sử dụng helper format (ví dụ trong `digest_template.py`) để trình bày đẹp.

### Helper/Utils
- Nếu cần nhiều lần format, tạo file `agent_template.py` chứa hàm `render_agent_report(macro, biz, tech, summary)`.
- Có thể tái sử dụng `send_md`/`push_telegram_msg` để gửi log.

### README Update
- Thêm mục “Multi-Agent Manual Trigger” mô tả:
  - Cách dùng `/agent <type>`
  - Các type hợp lệ
  - Vị trí lưu dữ liệu trên Redis
  - Cách đọc kết quả để chỉnh prompt

### Testing Checklist
- `/agent macro` (ADMIN) -> kiểm tra log & Redis key `agent:macro:*` tồn tại.
- `/agent all` -> cả 3 key + bundle xuất hiện, Telegram nhận log.
- Non-admin chạy `/agent` -> không phản hồi.

### Open Questions
1. Có cần lưu thêm metadata (thời gian crawl, người chạy) vào mỗi key để tiện audit?
2. Có muốn `/agent dump` lấy dữ liệu cũ mà không rerun?
