## Feature: /agent macro data pipeline

### Goals
- Khi admin chạy `/agent macro`, worker thu thập dữ liệu vĩ mô (tin tức, VNINDEX/VN30, báo cáo GSO), chuẩn hóa JSON và lưu vào Redis (`agent:macro:current`) thay thế stub hiện tại.
- Giữ nguyên contract bundle để Gateway và `/agentlog` có thể log ra nội dung chi tiết.

### Data Sources & Helpers
1. **News vĩ mô**: tái sử dụng kết quả từ `job_scan_news(MACRO)` hoặc DB/Redis liên quan để lấy danh sách bài mới (title, link, impact, timestamp).
2. **VNINDEX/VN30 snapshot**: trích logic hiện có trong `job_eod_summary` (biến `vni_data`, `v30_data`) thành helper có thể gọi lại bất cứ lúc nào.
3. **Báo cáo GSO (nso.gov.vn)**: tích hợp crawler theo script mẫu, gom dữ liệu theo tháng/quý gần nhất; chỉ cần trả JSON (month, year, title, summary, attachments) thay vì lưu file.

### Implementation Steps
1. **Kiến trúc module**
   - Tạo module mới (ví dụ `macro_agent.py`) chứa các helper: `collect_macro_news()`, `fetch_vnindex_vn30_snapshot()`, `crawl_gso_reports()`.
   - Giữ lời gọi Gemini trong worker (`call_gemini_safe`) để sinh `insights`/`ai_summary` dựa trên dữ liệu gom được.

2. **Cập nhật worker**
   - Trong `handle_agent_run`, khi scope có `macro`, gọi `asyncio.create_task(run_macro_agent(request_id, chat_id))`.
   - `run_macro_agent`:
     1. Gọi helper gom tin, snapshot VNINDEX/VN30, crawl GSO.
     2. Chuẩn hóa payload `macro_payload = {"agent": "macro", "request_id": ..., "generated_at": ..., "news": [...], "vnindex": {...}, "vn30": {...}, "gso_reports": [...], "insights": [...], "raw_data": {...}, "notes": ""}`.
     3. Lưu vào Redis bằng `save_agent_result("macro", macro_payload)` + cập nhật bundle (`bundle["agents"]["macro"] = macro_payload`).
     4. Gửi thông báo về admin (qua `push_telegram_msg`) với tóm tắt số lượng tin, biến động chỉ số, trạng thái crawler.

3. **Crawler GSO specifics**
   - Sử dụng logic `get_target_periods()` để xác định tháng cần crawl (anchor 1/4/7/10 đến hiện tại).
   - Cho phép cấu hình giới hạn (VD: chỉ lấy 1-2 tháng mới nhất để tránh quá tải).
   - Duyệt trang `https://www.nso.gov.vn/bai-top/{year}/{month:02d}/`, tìm bài có từ khóa "kinh tế" & "xã hội".
   - Parse nội dung chính (text paragraph) và danh sách file đính kèm (đưa link trực tiếp thay vì tải file).
   - Trả JSON `{"month": m, "year": y, "article_url": ..., "summary": [...], "attachments": [{"name": ..., "url": ...}]}`.

4. **Redis Keys & TTL**
   - Vẫn dùng `agent:macro:current` với TTL 24h (bằng `AGENT_RESULT_TTL`).
   - Nếu cần lưu lịch sử GSO nhiều tháng, cân nhắc key phụ `macro:gso:{year}{month}` (TTL dài ≥ 90 ngày) để tái sử dụng giữa các lần chạy.

5. **/agentlog mở rộng**
   - Không cần sửa lớn: command đã đọc JSON chung. Đảm bảo payload macro chứa trường `news`/`gso_reports` để admin có thể xem chi tiết.

6. **README**
   - Bổ sung mô tả rằng Macro agent hiện lấy tin RSS, snapshot VNINDEX/VN30 và báo cáo GSO.

### Testing Checklist
- `/agent macro` khi chưa có dữ liệu -> vẫn tạo payload rỗng nhưng valid.
- `/agent macro` sau khi helpers hoạt động -> Redis chứa JSON với news + vnindex + gso_reports (kiểm tra bằng `/agentlog macro`).
- Đảm bảo crawler GSO không crash khi thiếu bài (log warning, tiếp tục tháng khác).

### Open Questions
1. Có cần caching riêng để tránh gọi lại Gemini khi dữ liệu chưa đổi trong 24h?
2. Có muốn auto trigger macro agent sau mỗi lần `job_scan_news(MACRO)` hoàn tất để luôn sẵn dữ liệu cho admin?
