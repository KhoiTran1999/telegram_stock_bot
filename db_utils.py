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
            # Bảng lưu các cấu hình chung (ví dụ: trạng thái bảo trì)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value JSONB
                )
            """)
        conn.commit()

# Lấy toàn bộ danh sách theo dõi (cho alert_loop)
def get_all_watch():
    data = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, watch_list FROM bot_watch")
            for chat_id, watch_list in cur.fetchall():
                data[str(chat_id)] = {"list": watch_list or []}
    return data

# Lấy danh sách mã của 1 user
def get_watch_list_for_chat(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT watch_list FROM bot_watch WHERE chat_id = %s", (chat_id,))
            row = cur.fetchone()
    return row[0] if row else []

# Lưu danh sách mã (thêm hoặc cập nhật)
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

# ==============================
# CẤU HÌNH BOT (trạng thái bảo trì)
# ==============================
def get_bot_active() -> bool:
    """
    Đọc trạng thái BOT_ACTIVE từ bảng bot_config.
    Mặc định True nếu chưa có cấu hình.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = 'bot_active'")
            row = cur.fetchone()

    if not row or row[0] is None:
        # Chưa từng lưu -> mặc định là đang active
        return True

    value = row[0]
    # value là JSONB, mình lưu dạng {"active": true}
    if isinstance(value, dict) and "active" in value:
        return bool(value["active"])
    # fallback nếu sau này lỡ lưu kiểu True/False trần
    return bool(value)

def set_bot_active(is_active: bool):
    """Lưu trạng thái BOT_ACTIVE vào bảng bot_config."""
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
