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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_watch (
                    chat_id BIGINT PRIMARY KEY,
                    watch_list JSONB NOT NULL DEFAULT '[]'
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
