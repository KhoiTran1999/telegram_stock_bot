# db_utils.py
import os
import json
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]

def get_conn():
    return psycopg.connect(DATABASE_URL)

def init_db():
    """Tạo bảng nếu chưa có."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Bảng lưu danh sách mã theo dõi của từng user
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_watch (
                    chat_id BIGINT PRIMARY KEY,
                    watch_list JSONB NOT NULL DEFAULT '[]'
                )
            """)
            # Bảng lưu cấu hình chung (ví dụ: trạng thái bảo trì)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value JSONB
                )
            """)
            # 🆕 Bảng log lệnh người dùng (bao gồm cả /report)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS command_log (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    command TEXT NOT NULL,
                    used_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()

# ==============================
# LƯU DANH SÁCH & CẤU HÌNH
# ==============================

def get_all_watch():
    data = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, watch_list FROM bot_watch")
            for chat_id, watch_list in cur.fetchall():
                data[str(chat_id)] = {"list": watch_list or []}
    return data

def get_watch_list_for_chat(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT watch_list FROM bot_watch WHERE chat_id = %s", (chat_id,))
            row = cur.fetchone()
    return row[0] if row else []

def save_watch_list_for_chat(chat_id: int, watch_list):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_watch (chat_id, watch_list)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (chat_id)
                DO UPDATE SET watch_list = EXCLUDED.watch_list
            """, (chat_id, json.dumps(watch_list)))
        conn.commit()

def get_bot_active() -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = 'bot_active'")
            row = cur.fetchone()
    if not row or row[0] is None:
        return True
    value = row[0]
    if isinstance(value, dict) and "active" in value:
        return bool(value["active"])
    return bool(value)

def set_bot_active(is_active: bool):
    payload = {"active": bool(is_active)}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_config (key, value)
                VALUES ('bot_active', %s::jsonb)
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(payload),))
        conn.commit()

# ==============================
# 🧠 GHI NHẬT KÝ LỆNH
# ==============================

def log_command_usage(chat_id: int, command: str):
    """Ghi lại mỗi lần user dùng lệnh."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO command_log (chat_id, command, used_at) VALUES (%s, %s, NOW())",
                (chat_id, command),
            )
        conn.commit()

def get_command_stats():
    """Trả về thống kê số lần gọi theo ngày / tháng / tổng cộng cho từng lệnh."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    command,
                    COUNT(*) FILTER (WHERE used_at::date = CURRENT_DATE) AS day_count,
                    COUNT(*) FILTER (WHERE DATE_TRUNC('month', used_at) = DATE_TRUNC('month', CURRENT_DATE)) AS month_count,
                    COUNT(*) AS total_count
                FROM command_log
                GROUP BY command
                ORDER BY total_count DESC
            """)
            rows = cur.fetchall()
    stats = []
    for cmd, day, month, total in rows:
        stats.append({
            "command": cmd,
            "day": day,
            "month": month,
            "total": total,
        })
    return stats
