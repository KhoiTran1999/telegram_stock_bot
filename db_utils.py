# db_utils.py
import os
import json
import datetime

from psycopg import rows
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv
load_dotenv()

from news_seen_cache import (
    has_news_seen_redis,
    mark_news_seen_redis,
    get_news_seen_count_redis,
)
from redis_client import get_redis

REDIS_DEBUG = os.getenv("REDIS_DEBUG", "False").lower() in ("1", "true", "yes")

def redis_debug_log(message: str):
    """In log nhẹ khi REDIS_DEBUG bật."""
    if REDIS_DEBUG:
        print(f"[CACHE] {message}")


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

            # Tổng hợp những paid_users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS paid_users (
                chat_id     BIGINT PRIMARY KEY,
                expiry_date TIMESTAMPTZ NOT NULL, -- Ngày hết hạn gói Pro
                plan_name   TEXT DEFAULT 'pro'
                )
            """)


        conn.commit()

# ==========================================
# WATCHLIST
# ==========================================
def get_all_watch():
    """
    Trả về dict {str(chat_id): {'list': [...]}}.
    Ưu tiên đọc từ Redis, nếu miss / lỗi thì fallback DB và warm lại Redis.
    """
    data: dict[str, dict] = {}

    # 1) Thử đọc từ Redis trước
    try:
        r = get_redis()
        chat_ids = r.smembers("watch_chat_ids") or set()
        if chat_ids:
            for cid in chat_ids:
                raw = r.get(f"watch:{cid}")
                if raw:
                    try:
                        wl = json.loads(raw)
                    except Exception:
                        wl = []
                else:
                    wl = []
                # Redis trả cid là str, nhưng để chắc ăn convert int->str luôn
                data[str(int(cid))] = {"list": wl}
            return data
        else:
            redis_debug_log("Redis empty → fallback DB (watch_chat_ids = 0)")
    except Exception as e:
        # Nếu có lỗi Redis, thì bỏ qua và fallback DB
        redis_debug_log(f"Redis error in get_all_watch(): {e}")
        pass

    # 2) Fallback: đọc toàn bộ từ DB
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, watch_list FROM bot_watch")
            rows = cur.fetchall()

    for chat_id, watch_list in rows:
        data[str(chat_id)] = {"list": watch_list or []}

    # 3) Warm lại Redis từ DB (best-effort)
    try:
        r = get_redis()
        pipe = r.pipeline()
        pipe.delete("watch_chat_ids")
        for chat_id, watch_list in rows:
            key = f"watch:{chat_id}"
            wl = watch_list or [] # wl sẽ là [] (rỗng) hoặc ['HPG']

            # SỬA LỖI: Không kiểm tra 'if wl:',
            # user nào cũng phải được cache (kể cả list rỗng)
            pipe.set(key, json.dumps(wl))
            pipe.sadd("watch_chat_ids", chat_id) # Luôn thêm user vào set
        pipe.execute()
    except Exception:
        pass

    return data



def get_watch_list_for_chat(chat_id: int):
    """
    Lấy watchlist của 1 chat_id.
    Ưu tiên đọc từ Redis, nếu miss thì fallback DB và warm lại cache.
    """
    # 1) Thử lấy từ Redis trước
    try:
        r = get_redis()
        raw = r.get(f"watch:{chat_id}")
        if raw is not None:
            try:
                return json.loads(raw)
            except Exception:
                # Nếu parse lỗi thì bỏ qua, fallback DB
                pass
        else:
            redis_debug_log(f"Redis miss watch:{chat_id}")           
    except Exception as e:
        # Có lỗi Redis thì fallback DB
        redis_debug_log(f"Redis error watch:{chat_id}: {e}")
        pass

    # 2) Fallback: đọc từ DB như cũ
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT watch_list FROM bot_watch WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
    watch_list = row[0] if row else None

    # 3) Warm lại Redis nếu có dữ liệu
    if watch_list is not None:
        try:
            r = get_redis()
            key = f"watch:{chat_id}"
            if watch_list:
                r.set(key, json.dumps(watch_list))
                r.sadd("watch_chat_ids", chat_id)
            else:
                r.delete(key)
                r.srem("watch_chat_ids", chat_id)
        except Exception:
            pass

    return watch_list

def save_watch_list_for_chat(chat_id: int, watch_list):
    """
    Lưu watchlist vào DB và đồng thời cập nhật cache Redis.
    """
    # 1) Ghi vào DB (nguồn sự thật)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_watch (chat_id, watch_list)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (chat_id)
                DO UPDATE SET watch_list = EXCLUDED.watch_list
                """,
                (chat_id, json.dumps(watch_list)),
            )
        conn.commit()

    # 2) Cập nhật Redis cache (best-effort, không để lỗi Redis làm chết hàm)
    try:
        r = get_redis()
        key = f"watch:{chat_id}"
        
        # SỬA LỖI: Kiểm tra 'is not None' thay vì 'if list:'
        # List rỗng [] vẫn là một giá trị hợp lệ cần cache
        if watch_list is not None: 
            r.set(key, json.dumps(watch_list))
            r.sadd("watch_chat_ids", chat_id) # Luôn thêm user vào set
        else:
            # Trường hợp này ít xảy ra, nhưng để an toàn
            r.delete(key)
            r.srem("watch_chat_ids", chat_id)
    except Exception:
        pass


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
    Ưu tiên đọc từ Redis, nếu miss thì fallback DB và warm lại cache.
    Mặc định: cả hai đều True nếu chưa có record.
    """
    # 1) Thử lấy từ Redis
    try:
        r = get_redis()
        raw = r.get(f"news_pref:{chat_id}")
        if raw is not None:
            try:
                data = json.loads(raw)
                return {
                    "enable_specialized": bool(
                        data.get("enable_specialized", True)
                    ),
                    "enable_macro": bool(data.get("enable_macro", True)),
                }
            except Exception as e:
                # Nếu JSON lỗi thì bỏ qua, fallback DB
                redis_debug_log(f"Redis error news_pref:{chat_id}: {e}")
                pass
        else:
            redis_debug_log(f"Redis miss news_pref:{chat_id}")
    except Exception as e:
        # Redis lỗi -> fallback DB
        redis_debug_log(f"Redis error news_pref:{chat_id}: {e}")
        pass

    # 2) Fallback: đọc từ DB
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enable_specialized, enable_macro FROM news_pref WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()

    if not row:
        pref = {"enable_specialized": True, "enable_macro": True}
    else:
        enable_specialized, enable_macro = row
        pref = {
            "enable_specialized": bool(enable_specialized),
            "enable_macro": bool(enable_macro),
        }

    # 3) Warm lại Redis (best-effort)
    try:
        r = get_redis()
        r.set(f"news_pref:{chat_id}", json.dumps(pref))
    except Exception:
        pass

    return pref



def set_news_pref(
    chat_id: int,
    enable_specialized: bool | None = None,
    enable_macro: bool | None = None,
):
    """
    Cập nhật preference nhận tin tức cho 1 user.
    Ghi vào DB và sync sang Redis.
    """
    current = get_news_pref(chat_id)

    if enable_specialized is None:
        enable_specialized = current["enable_specialized"]
    if enable_macro is None:
        enable_macro = current["enable_macro"]

    # 1) Ghi vào DB
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO news_pref (chat_id, enable_specialized, enable_macro)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET enable_specialized = EXCLUDED.enable_specialized,
                    enable_macro       = EXCLUDED.enable_macro
                """,
                (chat_id, enable_specialized, enable_macro),
            )
        conn.commit()

    # 2) Cập nhật Redis (best-effort)
    pref = {
        "enable_specialized": bool(enable_specialized),
        "enable_macro": bool(enable_macro),
    }
    try:
        r = get_redis()
        r.set(f"news_pref:{chat_id}", json.dumps(pref))
    except Exception:
        pass

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

# ==========================================
# QUẢN LÝ USER TRẢ PHÍ (GÓI PRO)
# ==========================================
def add_paid_user(chat_id: int, days_to_add: int):
    """
    Thêm hoặc gia hạn Gói Pro cho user.
    - Nếu user đã hết hạn: Gia hạn `days_to_add` ngày kể từ HÔM NAY.
    - Nếu user vẫn còn hạn: Cộng dồn `days_to_add` ngày vào ngày hết hạn HIỆN TẠI.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Logic:
            # 1. INSERT... ON CONFLICT: Thử thêm mới
            # 2. DO UPDATE: Nếu đã tồn tại
            # 3. GREATEST(paid_users.expiry_date, NOW()): Lấy mốc thời gian
            #    là ngày hết hạn hiện tại, hoặc HÔM NAY (nếu đã hết hạn).
            # 4. + interval '1 day' * %s: Cộng thêm số ngày gia hạn.
            cur.execute(
                """
                INSERT INTO paid_users (chat_id, expiry_date)
                VALUES (%s, NOW() + interval '1 day' * %s)
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    expiry_date = GREATEST(paid_users.expiry_date, NOW()) + interval '1 day' * %s;
                """,
                (chat_id, days_to_add, days_to_add), # `days_to_add` được dùng 2 lần
            )
        conn.commit()


def is_user_pro(chat_id: int) -> bool:
    """
    Trả về True nếu user có trong bảng paid_users VÀ ngày hết hạn > NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 
                FROM paid_users
                WHERE chat_id = %s AND expiry_date > NOW()
                LIMIT 1;
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    
    # Nếu row không phải là None (tức là tìm thấy 1 dòng) -> True
    # Nếu row là None (không tìm thấy) -> False
    return bool(row)

def deactivate_paid_user(chat_id: int) -> int:
    """
    Ngưng hoạt động Gói Pro của user ngay lập tức.
    Hàm này set ngày hết hạn về quá khứ (NOW() - 1 giây).
    Trả về số dòng đã được cập nhật (0 hoặc 1).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE paid_users
                SET expiry_date = NOW() - interval '1 second'
                WHERE chat_id = %s;
                """,
                (chat_id,),
            )
            # cur.rowcount là số dòng bị ảnh hưởng bởi lệnh UPDATE
            updated_rows = cur.rowcount
        conn.commit()
    return updated_rows

def remove_paid_user(chat_id: int) -> int:
    """
    Xoá vĩnh viễn Gói Pro của user khỏi bảng.
    Trả về số dòng đã bị xoá (0 hoặc 1).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM paid_users
                WHERE chat_id = %s;
                """,
                (chat_id,),
            )
            # cur.rowcount là số dòng bị ảnh hưởng bởi lệnh DELETE
            deleted_rows = cur.rowcount
        conn.commit()
    return deleted_rows

# Dán hàm này vào file db_utils.py

def get_all_pro_chat_ids() -> set[int]:
    """
    Lấy MỘT TẬP (set) chứa chat_id của tất cả user
    đang có Gói Pro (còn hạn).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chat_id 
                FROM paid_users
                WHERE expiry_date > NOW();
                """,
            )
            rows = cur.fetchall()
    
    # Trả về set (tập hợp) để tra cứu O(1) (siêu nhanh)
    return {int(row[0]) for row in rows}

def get_all_paid_users_expiry() -> dict[int, datetime.datetime]:
    """
    Lấy MỘT DICT (dictionary) chứa {chat_id: expiry_date}
    của TẤT CẢ user trong bảng paid_users (bao gồm cả user đã hết hạn).
    """
    mapping = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chat_id, expiry_date 
                FROM paid_users;
                """
            )
            rows = cur.fetchall()
    
    # Trả về dict để tra cứu O(1)
    # expiry_date là kiểu TIMESTAMPTZ, nên nó sẽ là đối tượng datetime
    for row in rows:
        try:
            mapping[int(row[0])] = row[1] 
        except Exception:
            pass # Bỏ qua nếu parse lỗi
            
    return mapping