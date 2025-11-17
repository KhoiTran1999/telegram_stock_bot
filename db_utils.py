# db_utils.py
import os
import json
import datetime
import time

from psycopg import rows
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv
load_dotenv()

from news_seen_cache import (
    has_news_seen_redis,
    mark_news_seen_redis,
    get_news_seen_count_redis,
    canonicalize_link,
)
from redis_client import get_redis
import hashlib

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
            
            # Bảng lưu báo cáo phân tích đã gửi (tránh trùng)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analysis_report_seen (
                    id           SERIAL PRIMARY KEY,
                    symbol       TEXT, -- Chỉ để tham khảo
                    link         TEXT NOT NULL,
                    title        TEXT,
                    published_at TIMESTAMPTZ, -- Lấy từ cột 'date' của vnstock
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    
                    -- Dùng (link, published_at) làm khóa duy nhất như bạn yêu cầu
                    UNIQUE(link, published_at) 
                )
            """)

        conn.commit()

# ==========================================
# WATCHLIST
# ==========================================
def get_all_watch():
    """
    (ĐÃ SỬA) Trả về dict {str(chat_id): {'list': [...]}}.
    Ưu tiên đọc từ Redis, nếu miss cache của user nào thì
    gọi get_watch_list_for_chat() để fallback DB cho user đó.
    """
    data: dict[str, dict] = {}
    chat_ids_from_redis = set()

    # 1) Thử đọc từ Redis trước
    try:
        r = get_redis()
        chat_ids_from_redis = r.smembers("watch_chat_ids") or set()
        
        if chat_ids_from_redis:
            for cid_str in chat_ids_from_redis:
                try:
                    cid = int(cid_str)
                except Exception:
                    continue
                
                # Cố gắng lấy cache của user này
                raw = r.get(f"watch:{cid}")
                
                if raw is not None:
                    # === CACHE HIT ===
                    try:
                        wl = json.loads(raw)
                    except Exception:
                        wl = [] # Cache lỗi thì coi như rỗng
                    data[str(cid)] = {"list": wl}
                else:
                    # === CACHE MISS (Do bị Invalidate hoặc hết hạn) ===
                    # Gọi hàm "get" đơn lẻ (hàm này đã có logic fallback DB
                    # và tự động warm-up lại cache)
                    redis_debug_log(f"get_all_watch: Cache miss cho {cid}, gọi fallback...")
                    wl_fallback = get_watch_list_for_chat(cid) # Đã bao gồm DB + warm-up
                    if wl_fallback is not None:
                         data[str(cid)] = {"list": wl_fallback}
                    # (Nếu wl_fallback là None, tức là user không có trong DB)
            
            # Nếu data có nội dung thì trả về, không cần fallback toàn bộ
            if data:
                return data

    except Exception as e:
        redis_debug_log(f"Redis error in get_all_watch(): {e}")
        # Bỏ qua và fallback DB toàn bộ bên dưới

    # 2) Fallback: đọc toàn bộ từ DB (giữ nguyên logic của bạn)
    # (Trường hợp Redis sập, hoặc set "watch_chat_ids" rỗng)
    
    redis_debug_log("Redis empty/error → fallback DB (get_all_watch)")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, watch_list FROM bot_watch")
            rows = cur.fetchall()

    for chat_id, watch_list in rows:
        data[str(chat_id)] = {"list": watch_list or []}

    # 3) Warm lại Redis từ DB (best-effort, giữ nguyên)
    try:
        r = get_redis()
        r.delete("watch_chat_ids")
        for chat_id, watch_list in rows:
            key = f"watch:{chat_id}"
            wl = watch_list or []
            r.set(key, json.dumps(wl))
            r.sadd("watch_chat_ids", chat_id)
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
            
            # SỬA LỖI: List rỗng [] vẫn là một giá trị hợp lệ,
            # vẫn phải set key và add vào set toàn cục.
            
            # (watch_list có thể là [] hoặc ['HPG'])
            r.set(key, json.dumps(watch_list))
            r.sadd("watch_chat_ids", chat_id)
            
        except Exception:
            pass
    return watch_list

# TRONG DB_UTILS.PY

def save_watch_list_for_chat(chat_id: int, watch_list):
    """
    Lưu watchlist vào DB và đồng thời LÀM MẤT HIỆU LỰC (Invalidate) cache Redis.
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

    # 2) Xóa cache của user này khỏi Redis (Invalidate)
    try:
        r = get_redis()
        key = f"watch:{chat_id}"
        
        # Xóa cache chi tiết của user
        r.delete(key) 
        
        # Vẫn quản lý set "watch_chat_ids"
        if watch_list is not None and len(watch_list) > 0:
            # Nếu user có watchlist, đảm bảo họ có trong set
            r.sadd("watch_chat_ids", chat_id)
        else:
            # Nếu user xóa hết (list rỗng), xóa họ khỏi set (tùy chọn)
            # Hoặc cứ để trong set cũng không sao, vì get_all_watch sẽ xử lý
            pass 
            # Nếu muốn chặt chẽ:
            # if not watch_list:
            #     r.srem("watch_chat_ids", chat_id)
            
    except Exception as e:
        # Ghi log lỗi Redis nhưng không làm sập tiến trình
        redis_debug_log(f"Redis invalidate error watch:{chat_id}: {e}")
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
# NEWS: RSS SEEN + PREFERENCES + REDIS + POSTGRES
# ==========================================
def has_news_seen(feed_type: str, link: str) -> bool:
    """
    Kiểm tra xem bài RSS (feed_type, link) đã được xử lý chưa.

    Ưu tiên:
    1) Kiểm tra Redis (nhanh).
    2) Nếu miss -> kiểm tra Postgres (bảng news_seen).
       - Nếu tìm thấy -> warm lại Redis rồi trả True.
       - Nếu không -> False.

    Lưu ý:
    - Không log "Redis HIT" nữa để tránh spam log trong các loop RSS.
    - Chỉ log khi có lỗi Redis/DB.
    """
    ft = (feed_type or "").strip().upper()
    if not ft or not link:
        return False

    # Chuẩn hoá link để giảm trùng lặp do query tracking
    canonical_link = canonicalize_link(link)

    # 1) Hỏi Redis trước
    try:
        if has_news_seen_redis(ft, canonical_link):
            return True
    except Exception as e:
        redis_debug_log(f"[news_seen] Redis error in has_news_seen: {e}")

    # 2) Nếu Redis miss -> hỏi Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM news_seen
                    WHERE feed_type = %s
                      AND link = %s
                    LIMIT 1
                    """,
                    (ft, canonical_link),
                )
                row = cur.fetchone()
    except Exception as e:
        # Nếu DB có vấn đề, fallback: coi như chưa seen để không chặn tin
        redis_debug_log(f"[news_seen] DB error in has_news_seen: {e}")
        return False

    if row:
        # Warm lại Redis để lần sau nhanh hơn
        try:
            mark_news_seen_redis(ft, canonical_link)
        except Exception as e:
            redis_debug_log(f"[news_seen] Redis warm error: {e}")
        return True

    return False


def mark_news_seen(
    feed_type: str,
    link: str,
    guid: str | None = None,
    title: str | None = None,
    published=None,
) -> None:
    """
    Đánh dấu 1 bài RSS đã được xử lý.

    - Ghi vào Postgres (bảng news_seen) với canonical_link.
    - Set key tương ứng trong Redis (write-through cache).

    published có thể là:
    - datetime (tz-aware hoặc naive)
    - string (Postgres tự cast nếu hợp lệ)
    - None
    """
    ft = (feed_type or "").strip().upper()
    if not ft or not link:
        return

    canonical_link = canonicalize_link(link)

    # 1) Ghi vào Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO news_seen (feed_type, guid, link, title, published)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (feed_type, link) DO NOTHING
                    """,
                    (ft, guid, canonical_link, title, published),
                )
            conn.commit()
    except Exception as e:
        # Không raise để tránh làm hỏng luồng chính nếu DB lỗi
        redis_debug_log(f"[news_seen] DB error in mark_news_seen: {e}")

    # 2) Đánh dấu trong Redis
    try:
        mark_news_seen_redis(
            ft,
            canonical_link,
            title=title,
            published=published,
        )
    except Exception as e:
        redis_debug_log(f"[news_seen] Redis error in mark_news_seen: {e}")


def get_news_seen_count(feed_type: str) -> int:
    """
    Đếm số bài đã seen cho 1 feed_type dựa trên Redis.
    (chỉ dùng cho logic warm-up / debug).
    """
    return get_news_seen_count_redis(feed_type)


def cleanup_old_news_seen(retention_days: int = 180) -> int:
    """
    Xoá các bản ghi news_seen cũ hơn retention_days (mặc định ~ 6 tháng).

    - Dùng COALESCE(published, created_at) để xử lý cả case thiếu published.
    - Trả về số dòng đã xoá (rowcount).
    """
    try:
        retention_days = int(retention_days)
    except Exception:
        retention_days = 180

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Ví dụ: retention_days = 180 -> '180 days'
                interval_str = f"{retention_days} days"
                cur.execute(
                    """
                    DELETE FROM news_seen
                    WHERE COALESCE(published, created_at)
                          < NOW() - (%s)::INTERVAL
                    """,
                    (interval_str,),
                )
                deleted = cur.rowcount or 0
            conn.commit()
    except Exception as e:
        redis_debug_log(f"[news_seen] DB error in cleanup_old_news_seen: {e}")
        return 0

    return deleted


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

# Cache RAM (in-memory) cho các setting không đổi thường xuyên
_news_pref_cache: dict[int, dict] = {}
_news_pref_cache_time: float = 0.0

def get_all_news_pref(max_cache_age_sec: int = 60) -> dict[int, dict]:
    """
    (HÀM MỚI - TỐI ƯU N+1)
    Lấy toàn bộ setting tin tức của user, cache 60 giây trong RAM.
    Trả về dict: {chat_id: {"enable_specialized": bool, "enable_macro": bool}}
    """
    global _news_pref_cache, _news_pref_cache_time
    now = time.time()

    # 1. Dùng cache RAM (siêu nhanh) nếu còn hạn
    if (now - _news_pref_cache_time) < max_cache_age_sec and _news_pref_cache:
        return _news_pref_cache

    # 2. Cache cũ/rỗng -> Lấy từ DB (CHỈ 1 QUERY)
    redis_debug_log("[get_all_news_pref] Cache RAM rỗng/hết hạn. Đang lấy từ DB...")
    data = {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, enable_specialized, enable_macro FROM news_pref")
                rows = cur.fetchall()
        
        for chat_id, enable_specialized, enable_macro in rows:
            data[int(chat_id)] = {
                "enable_specialized": bool(enable_specialized),
                "enable_macro": bool(enable_macro),
            }
    except Exception as e:
        redis_debug_log(f"Lỗi get_all_news_pref: {e}")
        # Nếu lỗi, trả về cache cũ (nếu có) để bot tiếp tục chạy
        if _news_pref_cache:
            return _news_pref_cache
        return {} # Hoặc trả rỗng nếu chưa có cache

    # 3. Cập nhật cache RAM
    _news_pref_cache = data
    _news_pref_cache_time = now
    redis_debug_log(f"[get_all_news_pref] Đã cache {len(data)} user vào RAM.")
    return data
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

def get_user_pro_expiry(chat_id: int) -> datetime.datetime | None:
    """
    Lấy ngày hết hạn Pro (expiry_date) của user.
    Trả về datetime object (TIMESTAMPTZ) nếu có, ngược lại trả về None.
    Hàm này lấy cả user đã hết hạn.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT expiry_date 
                FROM paid_users
                WHERE chat_id = %s
                LIMIT 1;
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    
    # row[0] sẽ là expiry_date (datetime object) hoặc None
    return row[0] if row else None

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
#-----------------------------------
# ==========================================
# ANALYSIS REPORT SEEN (TÍNH NĂNG MỚI)
# ==========================================

# TTL 180 ngày, giống news_seen
REPORT_SEEN_TTL_SECONDS = 180 * 24 * 60 * 60

def _make_report_seen_key(link: str, pub_date_str: str) -> str:
    """
    Tạo key cache Redis.
    Dùng canonical_link (từ news_seen_cache) và pub_date_str 
    (theo yêu cầu của bạn) để đảm bảo tính duy nhất.
    """
    canonical_link = canonicalize_link(link)
    h = hashlib.sha256(f"{canonical_link}::{pub_date_str}".encode("utf-8")).hexdigest()[:32]
    return f"report_seen:{h}"

def has_report_seen(link: str, pub_date_str: str) -> bool:
    """
    Kiểm tra xem báo cáo (link + date) đã được xử lý chưa.
    1. Kiểm tra Redis
    2. Nếu miss -> Kiểm tra Postgres
    3. Nếu có -> Warm lại Redis
    """
    key = _make_report_seen_key(link, pub_date_str)
    
    # 1. Hỏi Redis trước
    try:
        r = get_redis()
        if r.exists(key):
            return True
    except Exception as e:
        redis_debug_log(f"[report_seen] Redis error in has_report_seen: {e}")

    # 2. Nếu Redis miss -> hỏi Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM analysis_report_seen
                    WHERE link = %s
                      AND published_at = %s
                    LIMIT 1
                    """,
                    (link, pub_date_str), # pub_date_str là 'YYYY-MM-DDTHH:MM:SSZ'
                )
                row = cur.fetchone()
    except Exception as e:
        redis_debug_log(f"[report_seen] DB error in has_report_seen: {e}")
        return False

    if row:
        # 3. Warm lại Redis
        try:
            r = get_redis()
            r.set(key, "1", ex=REPORT_SEEN_TTL_SECONDS)
        except Exception as e:
            redis_debug_log(f"[report_seen] Redis warm error: {e}")
        return True

    return False

def mark_report_seen(symbol: str, link: str, title: str, pub_date_str: str):
    """
    Đánh dấu 1 báo cáo đã được xử lý (Ghi vào DB và Redis).
    """
    key = _make_report_seen_key(link, pub_date_str)

    # 1. Ghi vào Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analysis_report_seen (symbol, link, title, published_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (link, published_at) DO NOTHING
                    """,
                    (symbol, link, title, pub_date_str),
                )
            conn.commit()
    except Exception as e:
        redis_debug_log(f"[report_seen] DB error in mark_report_seen: {e}")

    # 2. Đánh dấu trong Redis
    try:
        r = get_redis()
        r.set(key, "1", ex=REPORT_SEEN_TTL_SECONDS)
    except Exception as e:
        redis_debug_log(f"[report_seen] Redis error in mark_report_seen: {e}")