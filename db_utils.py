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
                    key   TEXT PRIMARY KEY,
                    value JSONB
                )
            """)

            # Log lệnh user (dùng cho thống kê /status)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS command_log (
                    id      SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    command TEXT   NOT NULL,
                    used_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)

            # Cache screener Value
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_value_cache (
                    symbol   TEXT PRIMARY KEY,
                    exchange TEXT,
                    industry TEXT,
                    pe       DOUBLE PRECISION,
                    pb       DOUBLE PRECISION,
                    roe      DOUBLE PRECISION,
                    floor    TEXT,
                    asset_proxy     DOUBLE PRECISION,
                    liquidity_proxy DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Log tin nhắn bot đã gửi (để xóa theo range)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_msg_log (
                    id         SERIAL PRIMARY KEY,
                    chat_id    BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    sent_at    TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)

            # Tin tức đã xử lý (RSS) – tránh gửi trùng
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_seen (
                    id        SERIAL PRIMARY KEY,
                    feed_type TEXT NOT NULL,           -- 'MACRO' hoặc 'SPECIALIZED'
                    guid      TEXT,
                    link      TEXT NOT NULL,
                    title     TEXT,
                    published TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(feed_type, link)
                )
            """)

            # Tùy chọn nhận tin tức theo từng user
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_pref (
                    chat_id            BIGINT PRIMARY KEY,
                    enable_specialized BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_macro       BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)

        conn.commit()

# ==========================================
# WATCHLIST
# ==========================================
def get_all_watch():
    """Trả về dict {str(chat_id): {'list': [...]}}."""
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
            cur.execute(
                "SELECT watch_list FROM bot_watch WHERE chat_id = %s",
                (chat_id,),
            )
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

# ==========================================
# BOT ACTIVE (BẢO TRÌ)
# ==========================================
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
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict) and "active" in parsed:
                return bool(parsed["active"])
            if isinstance(parsed, bool):
                return parsed
        except Exception:
            pass
    return True


def set_bot_active(active: bool):
    payload = {"active": bool(active)}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_config (key, value)
                VALUES ('bot_active', %s::jsonb)
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(payload),))
        conn.commit()

# ==========================================
# LOG COMMAND
# ==========================================
def log_command_usage(chat_id: int, command: str, admin_id: int | None = None):
    """
    Ghi log sử dụng lệnh.
    Nếu admin_id được truyền vào và chat_id == admin_id -> bỏ qua (không tính vào thống kê).
    """
    if admin_id is not None and chat_id == admin_id:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO command_log (chat_id, command) VALUES (%s, %s)",
                (chat_id, command),
            )
        conn.commit()


def get_command_stats():
    """
    Trả về list[dict]:
        {"command": "...", "day": "x", "month": "y", "total": "z"}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    command,
                    SUM(CASE WHEN used_at::date = CURRENT_DATE THEN 1 ELSE 0 END) AS day,
                    SUM(
                        CASE
                            WHEN date_trunc('month', used_at) = date_trunc('month', CURRENT_DATE)
                            THEN 1 ELSE 0
                        END
                    ) AS month,
                    COUNT(*) AS total
                FROM command_log
                GROUP BY command
                ORDER BY total DESC
            """)
            rows = cur.fetchall()

    stats = []
    for command, day, month, total in rows:
        stats.append(
            {
                "command": command,
                "day": str(day),
                "month": str(month),
                "total": str(total),
            }
        )
    return stats

# ==========================================
# LOG TIN NHẮN BOT ĐÃ GỬI
# ==========================================
def save_bot_message(chat_id: int, message_id: int):
    """Lưu lại message bot đã gửi để sau này xóa theo khoảng thời gian."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_msg_log (chat_id, message_id)
                VALUES (%s, %s)
            """, (chat_id, message_id))
        conn.commit()


def get_bot_messages_in_range(start_time, end_time):
    """Lấy danh sách (chat_id, message_id, sent_at) trong khoảng thời gian."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id, message_id, sent_at
                FROM bot_msg_log
                WHERE sent_at BETWEEN %s AND %s
                ORDER BY sent_at ASC
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

# ==========================================
# CACHE SCREENER VALUE
# ==========================================
def upsert_stock_value_batch(records):
    """
    Upsert nhiều dòng vào stock_value_cache.
    records: list[dict] với key:
        symbol, exchange, industry, pe, pb, roe, floor,
        asset_proxy, liquidity_proxy
    """
    if not records:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            params = []
            for r in records:
                params.append(
                    (
                        r.get("symbol"),
                        r.get("exchange"),
                        r.get("industry"),
                        r.get("pe"),
                        r.get("pb"),
                        r.get("roe"),
                        r.get("floor"),
                        r.get("asset_proxy"),
                        r.get("liquidity_proxy"),
                    )
                )
            cur.executemany("""
                INSERT INTO stock_value_cache (
                    symbol, exchange, industry, pe, pb, roe, floor,
                    asset_proxy, liquidity_proxy
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE
                SET
                    exchange = EXCLUDED.exchange,
                    industry = EXCLUDED.industry,
                    pe       = EXCLUDED.pe,
                    pb       = EXCLUDED.pb,
                    roe      = EXCLUDED.roe,
                    floor    = EXCLUDED.floor,
                    asset_proxy     = EXCLUDED.asset_proxy,
                    liquidity_proxy = EXCLUDED.liquidity_proxy,
                    updated_at      = NOW()
            """, params)
        conn.commit()


def load_stock_value_cache():
    """Trả về list[dict] từ stock_value_cache."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    symbol, exchange, industry, pe, pb, roe, floor,
                    asset_proxy, liquidity_proxy, updated_at
                FROM stock_value_cache
            """)
            rows = cur.fetchall()

    data = []
    for (
        sym, exchange, industry, pe, pb, roe, floor,
        asset_proxy, liquidity_proxy, updated_at
    ) in rows:
        data.append(
            {
                "symbol": sym,
                "exchange": exchange,
                "industry": industry,
                "pe": float(pe) if pe is not None else None,
                "pb": float(pb) if pb is not None else None,
                "roe": float(roe) if roe is not None else None,
                "floor": floor,
                "asset_proxy": float(asset_proxy) if asset_proxy is not None else None,
                "liquidity_proxy": float(liquidity_proxy) if liquidity_proxy is not None else None,
                "updated_at": updated_at,
            }
        )
    return data


def get_stock_value_cache_count() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_value_cache")
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def clear_stock_value_cache():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE stock_value_cache")
        conn.commit()

# ==========================================
# NEWS: RSS SEEN + PREFERENCES
# ==========================================
def has_news_seen(feed_type: str, link: str) -> bool:
    """Kiểm tra xem 1 bài báo (link) của loại feed_type đã được xử lý chưa."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM news_seen WHERE feed_type = %s AND link = %s LIMIT 1",
                (feed_type, link),
            )
            row = cur.fetchone()
    return row is not None


def mark_news_seen(
    feed_type: str,
    link: str,
    guid: str | None = None,
    title: str | None = None,
    published=None,
):
    """Đánh dấu 1 bài báo là đã xử lý (đã gửi hoặc đã warm-up)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO news_seen (feed_type, guid, link, title, published)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (feed_type, link) DO NOTHING
            """, (feed_type, guid, link, title, published))
        conn.commit()


def get_news_seen_count(feed_type: str) -> int:
    """Số lượng bài đã lưu dấu theo từng loại feed (MACRO / SPECIALIZED)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM news_seen WHERE feed_type = %s",
                (feed_type,),
            )
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_news_pref(chat_id: int) -> dict:
    """
    Lấy preference nhận tin tức của 1 user.
    Mặc định: cả hai đều True nếu chưa có record.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enable_specialized, enable_macro FROM news_pref WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()

    if not row:
        return {"enable_specialized": True, "enable_macro": True}

    enable_specialized, enable_macro = row
    return {
        "enable_specialized": bool(enable_specialized),
        "enable_macro": bool(enable_macro),
    }


def set_news_pref(
    chat_id: int,
    enable_specialized: bool | None = None,
    enable_macro: bool | None = None,
):
    """
    Cập nhật preference nhận tin:
    - Nếu None -> giữ nguyên giá trị cũ (hoặc True mặc định nếu chưa có record).
    """
    current = get_news_pref(chat_id)
    if enable_specialized is None:
        enable_specialized = current["enable_specialized"]
    if enable_macro is None:
        enable_macro = current["enable_macro"]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO news_pref (chat_id, enable_specialized, enable_macro)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET enable_specialized = EXCLUDED.enable_specialized,
                    enable_macro       = EXCLUDED.enable_macro
            """, (chat_id, enable_specialized, enable_macro))
        conn.commit()


def is_news_enabled_for_chat(chat_id: int, feed_type: str) -> bool:
    """Kiểm tra user có bật nhận loại tin feed_type hay không."""
    pref = get_news_pref(chat_id)
    if feed_type.upper() == "SPECIALIZED":
        return pref["enable_specialized"]
    if feed_type.upper() == "MACRO":
        return pref["enable_macro"]
    return True
