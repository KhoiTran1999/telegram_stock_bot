# db_utils.py
import os
import json
import datetime

import psycopg
from psycopg import rows
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv
load_dotenv()

from news_seen_cache import (
    has_news_seen_redis,
    mark_news_seen_redis,
    get_news_seen_count_redis,
)


# Lấy DATABASE_URL từ biến môi trường Render
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Tạo connection pool dùng chung cho toàn bộ service
# min_size: số connection tối thiểu
# max_size: số connection tối đa (tùy gói, Render free nên để 5–10 là ổn)
POOL = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    timeout=30,      # tối đa 30s chờ lấy connection trong pool
)

def get_conn():
    # Lấy connection từ pool (tái sử dụng, không mở/đóng liên tục nữa)
    return POOL.connection()

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

            # Bảng log mã đã thông báo BCTC (đảm bảo mỗi mã/quý chỉ báo 1 lần)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bctc_notified (
                    id          SERIAL PRIMARY KEY,
                    symbol      TEXT NOT NULL,
                    year        INTEGER NOT NULL,
                    quarter     INTEGER NOT NULL,
                    notified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(symbol, year, quarter)
                )
            """)

            # Hàng đợi gửi thông báo BCTC (crawl lúc 2h, gửi lúc 8h)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bctc_notify_queue (
                    id          SERIAL PRIMARY KEY,
                    symbol      TEXT NOT NULL,
                    year        INTEGER NOT NULL,
                    quarter     INTEGER NOT NULL,
                    notify_date DATE   NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(symbol, year, quarter, notify_date)
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
# NEWS: RSS SEEN + PREFERENCES + REDIS
# ==========================================
def has_news_seen(feed_type: str, link: str) -> bool:
    """
    Bọc lại hàm Redis để giữ API cũ cho alert_bot.py.
    """
    return has_news_seen_redis(feed_type, link)

def mark_news_seen(
    feed_type: str,
    link: str,
    guid: str | None = None,
    title: str | None = None,
    published=None,
):
    """
    Bọc lại hàm Redis. guid/title/published hiện chưa dùng trong Redis,
    nhưng giữ tham số để không phải sửa các chỗ gọi hàm.
    """
    mark_news_seen_redis(feed_type, link)

def get_news_seen_count(feed_type: str) -> int:
    """
    Dùng Redis để đếm số bài đã seen (cho logic warm-up).
    """
    return get_news_seen_count_redis(feed_type)
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

def cleanup_old_news_seen(max_age_days: int = 7) -> int:
    """
    Trước đây dọn DB, giờ không cần nữa vì Redis tự hết hạn theo TTL.
    Giữ hàm này để news_cleanup_loop vẫn gọi được mà không lỗi.
    """
    return 0

def is_news_enabled_for_chat(chat_id: int, feed_type: str) -> bool:
    """Kiểm tra user có bật nhận loại tin feed_type hay không."""
    pref = get_news_pref(chat_id)
    if feed_type.upper() == "SPECIALIZED":
        return pref["enable_specialized"]
    if feed_type.upper() == "MACRO":
        return pref["enable_macro"]
    return True

# ==========================================
# BÁO CÁO TÀI CHÍNH (BCTC)
# ==========================================

def has_bctc_notified(symbol: str, year: int, quarter: int) -> bool:
    """True nếu mã này đã được thông báo BCTC cho kỳ (year, quarter)."""
    sym = str(symbol).upper().strip()
    if not sym:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM bctc_notified
                WHERE symbol = %s AND year = %s AND quarter = %s
                LIMIT 1
                """,
                (sym, year, quarter),
            )
            row = cur.fetchone()
    return row is not None


def mark_bctc_notified(symbol: str, year: int, quarter: int):
    """Đánh dấu đã gửi thông báo BCTC cho mã / kỳ này (chỉ 1 lần / quý)."""
    sym = str(symbol).upper().strip()
    if not sym:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bctc_notified (symbol, year, quarter)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol, year, quarter)
                DO UPDATE SET notified_at = NOW()
                """,
                (sym, year, quarter),
            )
        conn.commit()


def add_bctc_queue(symbol: str, year: int, quarter: int, notify_date):
    """
    Thêm mã vào hàng đợi BCTC để gửi thông báo vào ngày notify_date (08:00).
    """
    sym = str(symbol).upper().strip()
    if not sym:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bctc_notify_queue (symbol, year, quarter, notify_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, year, quarter, notify_date)
                DO NOTHING
                """,
                (sym, year, quarter, notify_date),
            )
        conn.commit()


def get_bctc_queue_by_date(notify_date):
    """Lấy danh sách (symbol, year, quarter) cần gửi vào ngày notify_date."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, year, quarter
                FROM bctc_notify_queue
                WHERE notify_date = %s
                """,
                (notify_date,),
            )
            return cur.fetchall()


def clear_bctc_queue_entry(symbol: str, year: int, quarter: int, notify_date):
    """Xóa 1 entry khỏi hàng đợi sau khi gửi xong."""
    sym = str(symbol).upper().strip()
    if not sym:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM bctc_notify_queue
                WHERE symbol = %s AND year = %s AND quarter = %s AND notify_date = %s
                """,
                (sym, year, quarter, notify_date),
            )
        conn.commit()

# ----------------------------------------------
# BACKUP / RESTORE CORE DATA
# ----------------------------------------------

# Các bảng "cốt lõi" cần backup/restore khi đổi DB
CORE_TABLES = [
    "bot_watch",       # watchlist mỗi user
    "news_pref",       # bật/tắt tin tức
    "bot_config",      # cấu hình chung (BOT_ACTIVE, v.v.)
    "bctc_notified",   # đã notify BCTC quý nào
]


def export_core_data():
    """
    Export dữ liệu core ra dict Python, sau đó có thể json.dump ra file.
    Cấu trúc:
    {
        "version": 1,
        "exported_at": "...",
        "tables": {
            "bot_watch": [...],
            "news_pref": [...],
            "bot_config": [...],
            "bctc_notified": [...]
        }
    }
    """
    payload = {
        "version": 1,
        "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        "tables": {},
    }

    with get_conn() as conn:
        # row_factory=dict_row => mỗi dòng là 1 dict
        with conn.cursor(row_factory=rows.dict_row) as cur:
            for tbl in CORE_TABLES:
                cur.execute(f"SELECT * FROM {tbl}")
                rows_data = cur.fetchall()
                payload["tables"][tbl] = rows_data

    return payload


def import_core_data(payload: dict, mode: str = "replace"):
    """
    Restore dữ liệu core từ dict đã export_core_data().
    mode = "replace": truncate 4 bảng core trước rồi insert lại từ backup.
    """
    tables = payload.get("tables", {}) or {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) Nếu replace thì xóa sạch dữ liệu cũ ở 4 bảng core
            if mode == "replace":
                # Truncate theo thứ tự, tránh phụ thuộc FK (ở đây không có FK nên khá thoải mái)
                for tbl in CORE_TABLES:
                    if tbl in tables:
                        cur.execute(f"TRUNCATE {tbl}")

            # 2) bot_watch
            for row in tables.get("bot_watch", []):
                chat_id = row["chat_id"]
                watch_list = row.get("watch_list", [])

                # watch_list có thể là list/dict hoặc string JSON -> chuẩn hóa thành string JSON
                if isinstance(watch_list, str):
                    raw_json = watch_list
                else:
                    raw_json = json.dumps(watch_list)

                cur.execute(
                    """
                    INSERT INTO bot_watch (chat_id, watch_list)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (chat_id)
                    DO UPDATE SET watch_list = EXCLUDED.watch_list
                    """,
                    (chat_id, raw_json),
                )

            # 3) news_pref
            for row in tables.get("news_pref", []):
                cur.execute(
                    """
                    INSERT INTO news_pref (chat_id, enable_specialized, enable_macro)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chat_id)
                    DO UPDATE
                    SET enable_specialized = EXCLUDED.enable_specialized,
                        enable_macro       = EXCLUDED.enable_macro
                    """,
                    (
                        row["chat_id"],
                        row["enable_specialized"],
                        row["enable_macro"],
                    ),
                )

            # 4) bot_config (value là JSONB)
            for row in tables.get("bot_config", []):
                key = row["key"]
                value = row.get("value")

                if value is None:
                    # cho phép NULL
                    cur.execute(
                        """
                        INSERT INTO bot_config (key, value)
                        VALUES (%s, NULL)
                        ON CONFLICT (key)
                        DO UPDATE SET value = EXCLUDED.value
                        """,
                        (key,),
                    )
                else:
                    # value có thể là dict/list/str -> convert thành JSON string cho chắc
                    if isinstance(value, str):
                        json_value = value
                    else:
                        json_value = json.dumps(value)

                    cur.execute(
                        """
                        INSERT INTO bot_config (key, value)
                        VALUES (%s, %s::jsonb)
                        ON CONFLICT (key)
                        DO UPDATE SET value = EXCLUDED.value
                        """,
                        (key, json_value),
                    )

            # 5) bctc_notified
            for row in tables.get("bctc_notified", []):
                sym = row["symbol"]
                year = row["year"]
                quarter = row["quarter"]
                notified_at = row.get("notified_at")

                cur.execute(
                    """
                    INSERT INTO bctc_notified (symbol, year, quarter, notified_at)
                    VALUES (%s, %s, %s, COALESCE(%s, NOW()))
                    ON CONFLICT (symbol, year, quarter)
                    DO UPDATE SET notified_at = EXCLUDED.notified_at
                    """,
                    (sym, year, quarter, notified_at),
                )

        conn.commit()

# ----------------------------------------------
# THEO DÕI THÁNG ĐÃ RESTORE CORE
# ----------------------------------------------

def _normalize_bot_config_value(value):
    """
    Chuẩn hóa value lấy từ bot_config:
    - Nếu là dict: trả lại nguyên dict.
    - Nếu là str: cố parse json -> dict.
    - Ngược lại: trả về {}.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def get_last_restore_month() -> str | None:
    """
    Lấy tháng gần nhất đã chạy /restore_core, dạng 'YYYY-MM'.
    Nếu chưa từng restore -> trả về None.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = 'last_restore_core'")
            row = cur.fetchone()

    if not row or row[0] is None:
        return None

    data = _normalize_bot_config_value(row[0])
    month = data.get("month")
    if isinstance(month, str):
        return month
    return None


def mark_restore_done_now():
    """
    Ghi nhận rằng /restore_core vừa được chạy thành công,
    lưu lại tháng hiện tại vào bot_config.
    """
    now = datetime.datetime.utcnow()
    month_key = now.strftime("%Y-%m")
    payload = {
        "month": month_key,
        "restored_at": now.isoformat() + "Z",
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_config (key, value)
                VALUES ('last_restore_core', %s::jsonb)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value
                """,
                (json.dumps(payload),),
            )
        conn.commit()

