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
                        # 🧮 Bảng cache dữ liệu screener Value (P/E, P/B, ROE)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_value_cache (
                    symbol   TEXT PRIMARY KEY,
                    exchange TEXT,
                    industry TEXT,
                    pe       DOUBLE PRECISION,
                    pb       DOUBLE PRECISION,
                    roe      DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    return row[0] if row else None

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
# 📊 CACHE SCREENER VALUE
# ==============================

def upsert_stock_value_batch(records):
    """
    Ghi / cập nhật 1 batch dữ liệu screener Value vào bảng stock_value_cache.
    Mỗi record: {symbol, exchange, industry, pe, pb, roe}
    """
    if not records:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Đảm bảo bảng tồn tại (phòng khi init_db chưa chạy)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_value_cache (
                    symbol   TEXT PRIMARY KEY,
                    exchange TEXT,
                    industry TEXT,
                    pe       DOUBLE PRECISION,
                    pb       DOUBLE PRECISION,
                    roe      DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            for r in records:
                cur.execute(
                    """
                    INSERT INTO stock_value_cache (symbol, exchange, industry, pe, pb, roe, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (symbol)
                    DO UPDATE SET
                        exchange   = EXCLUDED.exchange,
                        industry   = EXCLUDED.industry,
                        pe         = EXCLUDED.pe,
                        pb         = EXCLUDED.pb,
                        roe        = EXCLUDED.roe,
                        updated_at = NOW()
                    """,
                    (
                        r.get("symbol"),
                        r.get("exchange"),
                        r.get("industry"),
                        r.get("pe"),
                        r.get("pb"),
                        r.get("roe"),
                    ),
                )
        conn.commit()


def load_stock_value_cache():
    """
    Đọc toàn bộ dữ liệu từ bảng stock_value_cache.
    Trả về list[dict] mỗi phần tử: {symbol, exchange, industry, pe, pb, roe, updated_at}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT symbol, exchange, industry, pe, pb, roe, updated_at
                    FROM stock_value_cache
                """)
            except psycopg.errors.UndefinedTable:
                return []

            rows = cur.fetchall()

    data = []
    for sym, exchange, industry, pe, pb, roe, updated_at in rows:
        data.append(
            {
                "symbol": sym,
                "exchange": exchange,
                "industry": industry,
                "pe": float(pe) if pe is not None else None,
                "pb": float(pb) if pb is not None else None,
                "roe": float(roe) if roe is not None else None,
                "updated_at": updated_at,
            }
        )
    return data


def get_stock_value_cache_count() -> int:
    """Trả về số dòng hiện có trong stock_value_cache (0 nếu chưa có bảng)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(*) FROM stock_value_cache")
            except psycopg.errors.UndefinedTable:
                return 0
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0

def clear_stock_value_cache():
    """
    Xoá toàn bộ dữ liệu trong bảng stock_value_cache.
    Nếu bảng chưa tồn tại thì bỏ qua.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("TRUNCATE TABLE stock_value_cache;")
            except psycopg.errors.UndefinedTable:
                # Bảng chưa tồn tại thì coi như đã clear xong
                return
        conn.commit()


# ==============================
# 🧠 GHI NHẬT KÝ LỆNH
# ==============================

def log_command_usage(chat_id: int, command: str, admin_id: int = None):
    """Ghi lại mỗi lần user dùng lệnh (bỏ qua admin)."""
    if admin_id is not None and chat_id == admin_id:
        return  # ❌ bỏ qua log nếu là admin

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

# ==============================
# 🧠 LƯU & XOÁ THEO KHOẢNG THỜI GIAN
# ==============================

def save_bot_message(chat_id: int, message_id: int):
    """Lưu log mỗi tin nhắn bot gửi."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_msg_log (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    sent_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "INSERT INTO bot_msg_log (chat_id, message_id) VALUES (%s, %s)",
                (chat_id, message_id),
            )
        conn.commit()


def get_bot_messages_in_range(start_time, end_time):
    """Lấy danh sách tin bot gửi trong khoảng thời gian."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id, message_id
                FROM bot_msg_log
                WHERE sent_at BETWEEN %s AND %s
            """, (start_time, end_time))
            return cur.fetchall()


def delete_bot_messages_in_range(start_time, end_time):
    """Xoá record log khỏi DB sau khi xoá trên Telegram."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM bot_msg_log
                WHERE sent_at BETWEEN %s AND %s
            """, (start_time, end_time))
        conn.commit()

